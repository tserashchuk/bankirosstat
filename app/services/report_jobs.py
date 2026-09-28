from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy.exc import InvalidRequestError
from sqlmodel import Session, select

from app.database import engine
from app.models import (
    BackgroundJob,
    BackgroundJobStatus,
    Employee,
    InternalProject,
    ProjectSource,
    RagStatus,
    ReportHistory,
)
from app.repositories import get_project_employees, get_project_with_sources, list_projects_with_sources
from app.services.aggregator import collect_project_data
from app.services.collectors.collect_log import log_snapshot_summary
from app.services.portfolio_collect import collect_portfolio_snapshots
from app.services.jobs.store import (
    JobCancelledError,
    finish_job_error,
    finish_job_ok,
    get_job,
    mark_running,
    raise_if_job_cancelled,
    update_progress,
)
from app.services.llm import generate_portfolio_summary, generate_report
from app.services.llm.youtrack_links import (
    linkify_youtrack_ids,
    merge_youtrack_link_rules,
)
from app.services.llm.prompts import (
    REPORT_FORMAT_BRIEF_PROGRESS,
    REPORT_FORMAT_MILESTONE_GAMMA,
    REPORT_FORMAT_MILESTONE_SYNC,
    REPORT_FORMAT_STATUS,
    REPORT_FORMAT_STATUS_RESULTS,
    should_generate_portfolio_summary,
)
from app.services.llm.generator import format_llm_error

logger = logging.getLogger(__name__)


@dataclass
class ProjectReportResult:
    project_id: uuid.UUID
    project_name: str
    report_text: str
    rag_status: RagStatus
    snapshot: dict
    error: str | None = None


@dataclass
class _ProjectTarget:
    """Данные проекта, отвязанные от SQLAlchemy Session (для фонового воркера)."""

    id: uuid.UUID
    name: str
    description: str | None
    sources: list[ProjectSource]
    employees: list[Employee]


@dataclass
class _ReportRunState:
    """Внутреннее состояние выполнения задачи внутри воркера."""

    job_id: str
    model_key: str
    report_format: str
    project_id: Optional[uuid.UUID]
    project_reports: list[ProjectReportResult] = field(default_factory=list)
    portfolio_summary: Optional[str] = None
    report_text: str = ""
    rag_status: RagStatus = RagStatus.GREEN
    snapshot: dict[str, Any] = field(default_factory=dict)


def _load_report_targets(
    session: Session,
    project_id: uuid.UUID | None,
) -> list[_ProjectTarget]:
    """Загружает проекты и отвязывает ORM-объекты от сессии до её закрытия."""
    if project_id:
        project, sources = get_project_with_sources(session, project_id)
        if not project:
            raise ValueError("Проект не найден")
        employees = get_project_employees(session, project_id)
        rows = [(project, sources, employees)]
    else:
        rows = list_projects_with_sources(session)

    targets: list[_ProjectTarget] = []
    for project, sources, employees in rows:
        pid = project.id
        pname = project.name
        pdesc = project.description
        src_list = list(sources)
        emp_list = list(employees)
        for src in src_list:
            _ = (
                src.id,
                src.source_type,
                src.credentials_encrypted,
                src.label,
                src.project_id,
            )
        for emp in emp_list:
            _ = (
                emp.id,
                emp.full_name,
                emp.yandex_email,
                emp.imap_password_encrypted,
                emp.calendar_password_encrypted,
            )
        session.expunge(project)
        for src in src_list:
            session.expunge(src)
        for emp in emp_list:
            # В редких случаях Employee уже отвязан от сессии (например, из другого запроса).
            # Игнорируем такие экземпляры, нам нужны только их поля.
            try:
                session.expunge(emp)
            except InvalidRequestError:
                pass
        targets.append(
            _ProjectTarget(
                id=pid,
                name=pname,
                description=pdesc,
                sources=src_list,
                employees=emp_list,
            )
        )
    return targets


def _targets_for_collect(
    targets: list[_ProjectTarget],
) -> list[tuple[Any, list[ProjectSource], list[Employee]]]:
    """Совместимость с collect_portfolio_snapshots / build_project_profiles."""
    return [
        (
            SimpleNamespace(id=t.id, name=t.name, description=t.description),
            t.sources,
            t.employees,
        )
        for t in targets
    ]


_RAG_PRIORITY = {RagStatus.RED: 0, RagStatus.AMBER: 1, RagStatus.GREEN: 2}


def _worst_rag(current: RagStatus, new: RagStatus) -> RagStatus:
    return new if _RAG_PRIORITY[new] < _RAG_PRIORITY[current] else current


def _summary_items_from_reports(project_reports: list[ProjectReportResult]) -> list[dict]:
    items: list[dict] = []
    for pr in project_reports:
        if pr.error:
            items.append({"project": pr.project_name, "rag": pr.rag_status.value, "error": pr.error})
            continue
        text = (pr.report_text or "").strip()
        if len(text) > 3500:
            text = text[:3500] + "\n… (обрезано)"
        items.append({"project": pr.project_name, "rag": pr.rag_status.value, "report": text})
    return items


def _build_previous_reports_context(
    session: Session,
    project_id: uuid.UUID,
    *,
    limit: int = 3,
    scan_limit: int = 120,
) -> list[dict[str, Any]]:
    """Последние сохранённые отчёты по проекту (включая портфельные, где проект был внутри snapshot)."""
    rows = session.exec(
        select(ReportHistory).order_by(ReportHistory.created_at.desc()).limit(scan_limit)
    ).all()
    result: list[dict[str, Any]] = []
    for rep in rows:
        linked = rep.project_id == project_id
        if not linked:
            snap = rep.raw_data_snapshot or {}
            for p in snap.get("projects") or []:
                try:
                    if uuid.UUID(str(p.get("project_id"))) == project_id:
                        linked = True
                        break
                except (TypeError, ValueError):
                    continue
        if not linked:
            continue
        excerpt = (rep.generated_text or "").strip()
        if len(excerpt) > 700:
            excerpt = excerpt[:700] + "\n… (обрезано)"
        result.append(
            {
                "created_at": rep.created_at.strftime("%Y-%m-%d %H:%M"),
                "title": (rep.title or "").strip(),
                "rag_status": rep.rag_status.value,
                "excerpt": excerpt,
            }
        )
        if len(result) >= limit:
            break
    return result


def _combine_reports(
    project_reports: list[ProjectReportResult],
    portfolio_summary: str | None = None,
    *,
    report_format: str = "brief_progress",
) -> str:
    if not project_reports:
        return ""
    if len(project_reports) == 1:
        pr = project_reports[0]
        text = (pr.report_text or "").strip()
        if not text and pr.error:
            return f"# {pr.project_name}\n\n_Ошибка генерации: {pr.error}_"
        if portfolio_summary and portfolio_summary.strip():
            rag_emoji = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}.get(
                pr.rag_status.value, ""
            )
            title = (
                f"# Статус: результаты — {pr.project_name} {rag_emoji}"
                if report_format == REPORT_FORMAT_STATUS_RESULTS
                else f"# {pr.project_name} {rag_emoji}"
            )
            return "\n".join(
                [
                    title,
                    "",
                    "## 📋 Саммари",
                    "",
                    portfolio_summary.strip(),
                    "",
                    "---",
                    "",
                    text,
                ]
            )
        return pr.report_text or ""

    n = len(project_reports)
    if report_format == REPORT_FORMAT_STATUS:
        title = f"# Статусы по проектам ({n})"
    elif report_format == REPORT_FORMAT_STATUS_RESULTS:
        title = f"# Статус: результаты по проектам ({n})"
    elif report_format == REPORT_FORMAT_BRIEF_PROGRESS:
        title = f"# Краткий отчёт по проектам ({n})"
    elif report_format == REPORT_FORMAT_MILESTONE_SYNC:
        title = f"# Синхронизация с milestone ({n})"
    elif report_format == REPORT_FORMAT_MILESTONE_GAMMA:
        title = f"# Milestone + план презы ({n})"
    else:
        title = f"# Еженедельный отчёт по портфелю ({n} проектов)"
    lines = [
        title,
        "",
        f"_Сформировано: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC_",
        "",
    ]
    if portfolio_summary and portfolio_summary.strip():
        lines.extend(
            [
                "## 📋 Саммари по портфелю",
                "",
                portfolio_summary.strip(),
                "",
                "---",
                "",
            ]
        )
    for pr in project_reports:
        rag_emoji = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}.get(pr.rag_status.value, "")
        lines.append("---")
        lines.append("")
        lines.append(f"## {pr.project_name} {rag_emoji}")
        lines.append("")
        if pr.error:
            lines.append(f"_Не удалось сгенерировать: {pr.error}_")
        else:
            lines.append(pr.report_text.strip())
        lines.append("")
    return "\n".join(lines).strip()


def _result_payload(state: _ReportRunState) -> dict[str, Any]:
    return {
        "report_text": state.report_text,
        "rag_status": state.rag_status.value,
        "report_format": state.report_format,
        "portfolio_summary": state.portfolio_summary,
        "snapshot": state.snapshot,
        "project_reports": [
            {
                "project_id": str(pr.project_id),
                "project_name": pr.project_name,
                "rag_status": pr.rag_status.value,
                "report_text": pr.report_text,
                "snapshot": pr.snapshot,
                "error": pr.error,
            }
            for pr in state.project_reports
        ],
    }


async def run_report_job(job_id: str) -> None:
    """Тяжёлая работа: вызывается из ARQ-воркера. Прогресс/результат пишет в BackgroundJob."""
    job = get_job(job_id)
    if not job:
        logger.error("Report job %s not found in DB", job_id)
        return

    payload = job.payload or {}
    model_key = str(payload.get("model_key") or "")
    report_format = str(payload.get("report_format") or "brief_progress")
    raw_project_id = payload.get("project_id")
    project_id: Optional[uuid.UUID] = None
    if raw_project_id:
        try:
            project_id = uuid.UUID(str(raw_project_id))
        except ValueError:
            project_id = None

    state = _ReportRunState(
        job_id=job_id,
        model_key=model_key,
        report_format=report_format,
        project_id=project_id,
    )

    mark_running(job_id, "Сбор данных по проектам...")

    try:
        raise_if_job_cancelled(job_id)

        with Session(engine) as session:
            targets = _load_report_targets(session, state.project_id)

        if not targets:
            raise ValueError("Нет проектов — создайте проект в разделе «Проекты»")

        total = len(targets)
        portfolio_rag = RagStatus.GREEN
        portfolio_link_rules = None

        update_progress(job_id, "Сбор и распределение данных по проектам...")
        raise_if_job_cancelled(job_id)

        collect_rows = _targets_for_collect(targets)
        with Session(engine) as collect_session:
            if state.project_id is None and total > 0:
                snapshots_by_id = await collect_portfolio_snapshots(
                    collect_rows, session=collect_session
                )
            else:
                snapshots_by_id = {}
                for target in targets:
                    snapshots_by_id[target.id] = await collect_project_data(
                        target.sources,
                        target.employees,
                        session=collect_session,
                        project_id=target.id,
                    )

        for target in targets:
            snap = snapshots_by_id.get(target.id, {})
            if snap:
                log_snapshot_summary(snap, project=target.name)

        with Session(engine) as history_session:
            previous_reports_by_project = {
                t.id: _build_previous_reports_context(history_session, t.id)
                for t in targets
            }

        for index, target in enumerate(targets, start=1):
            raise_if_job_cancelled(job_id)
            snapshot = snapshots_by_id.get(target.id, {})
            update_progress(
                job_id,
                f"[{index}/{total}] {target.name}: генерация ИИ...",
            )
            await asyncio.sleep(0)

            try:
                text, rag = await generate_report(
                    state.model_key,
                    target.name,
                    snapshot,
                    state.report_format,
                    target.description,
                    previous_reports_by_project.get(target.id, []),
                )
                text = linkify_youtrack_ids(text, snapshot=snapshot)
                state.project_reports.append(
                    ProjectReportResult(
                        project_id=target.id,
                        project_name=target.name,
                        report_text=text,
                        rag_status=rag,
                        snapshot=snapshot,
                    )
                )
                portfolio_rag = _worst_rag(portfolio_rag, rag)
            except Exception as e:
                logger.error(
                    "Report generation failed for project %s (job %s, model %s): %s",
                    target.name,
                    job_id,
                    state.model_key,
                    e,
                )
                state.project_reports.append(
                    ProjectReportResult(
                        project_id=target.id,
                        project_name=target.name,
                        report_text="",
                        rag_status=RagStatus.RED,
                        snapshot=snapshot,
                        error=format_llm_error(e),
                    )
                )
                portfolio_rag = _worst_rag(portfolio_rag, RagStatus.RED)

            await asyncio.sleep(0)

        portfolio_summary: Optional[str] = None
        if should_generate_portfolio_summary(
            state.report_format,
            all_projects=state.project_id is None,
            project_count=len(state.project_reports),
        ):
            update_progress(job_id, "Саммари по портфелю...")
            raise_if_job_cancelled(job_id)
            await asyncio.sleep(0)
            summary_items = _summary_items_from_reports(state.project_reports)
            try:
                portfolio_summary = await generate_portfolio_summary(
                    state.model_key,
                    summary_items,
                    report_format=state.report_format,
                )
                if portfolio_link_rules is None:
                    portfolio_link_rules = merge_youtrack_link_rules(
                        *(pr.snapshot for pr in state.project_reports)
                    )
                portfolio_summary = linkify_youtrack_ids(
                    portfolio_summary, rules=portfolio_link_rules
                )
            except Exception as e:
                logger.exception("Portfolio summary failed (job %s)", job_id)
                portfolio_summary = (
                    f"_Не удалось сгенерировать саммари: {format_llm_error(e)}_"
                )

        if portfolio_link_rules is None:
            portfolio_link_rules = merge_youtrack_link_rules(
                *(pr.snapshot for pr in state.project_reports)
            )

        state.portfolio_summary = portfolio_summary
        state.report_text = _combine_reports(
            state.project_reports,
            portfolio_summary,
            report_format=state.report_format,
        )
        state.rag_status = portfolio_rag
        state.snapshot = {
            "portfolio": True,
            "report_format": state.report_format,
            "portfolio_summary": portfolio_summary,
            "projects": [
                {
                    "project_id": str(pr.project_id),
                    "project_name": pr.project_name,
                    "rag_status": pr.rag_status.value,
                    "report_text": pr.report_text,
                    "snapshot": pr.snapshot,
                    "error": pr.error,
                }
                for pr in state.project_reports
            ],
        }

        raise_if_job_cancelled(job_id)
        finish_job_ok(
            job_id,
            _result_payload(state),
            progress=f"Готово: {total} проект(ов)",
        )

    except JobCancelledError:
        logger.info("Report job %s cancelled", job_id)
    except Exception as e:
        logger.exception("Report job %s failed", job_id)
        finish_job_error(job_id, format_llm_error(e))


def save_report_to_history(
    session: Session,
    model_key: str,
    snapshot: dict,
    text: str,
    rag: RagStatus,
    *,
    project_id: uuid.UUID | None = None,
    title: str | None = None,
) -> ReportHistory:
    report = ReportHistory(
        project_id=project_id,
        title=title,
        model_used=model_key,
        raw_data_snapshot=snapshot,
        generated_text=text,
        rag_status=rag,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    return report


def _anchor_project_id_from_job(
    session: Session,
    job: BackgroundJob,
    snapshot: dict,
) -> uuid.UUID | None:
    """Проект для FK: явный из задачи или первый из портфельного snapshot."""
    payload = job.payload or {}
    raw_pid = payload.get("project_id")
    if raw_pid:
        try:
            return uuid.UUID(str(raw_pid))
        except ValueError:
            pass
    for entry in snapshot.get("projects") or []:
        try:
            return uuid.UUID(str(entry.get("project_id")))
        except (TypeError, ValueError):
            continue
    first = session.exec(select(InternalProject).limit(1)).first()
    return first.id if first else None


def _rag_from_str(value: str | None) -> RagStatus:
    if not value:
        return RagStatus.AMBER
    try:
        return RagStatus(value)
    except ValueError:
        return RagStatus.AMBER


def save_combined_from_job(session: Session, job: BackgroundJob) -> str | None:
    """Сохраняет в историю один объединённый отчёт (как на дашборде)."""
    if job.status != BackgroundJobStatus.DONE or not job.result:
        return None
    text = (job.result.get("report_text") or "").strip()
    if not text:
        return None
    snapshot = dict(job.result.get("snapshot") or {})
    fmt = job.result.get("report_format") or (job.payload or {}).get("report_format")
    if fmt and not snapshot.get("report_format"):
        snapshot["report_format"] = fmt
    model_key = str((job.payload or {}).get("model_key") or "")
    anchor_id = _anchor_project_id_from_job(session, job, snapshot)
    if not anchor_id:
        return None
    title = (job.title or "").strip() or None
    report = save_report_to_history(
        session,
        model_key,
        snapshot,
        text,
        _rag_from_str(job.result.get("rag_status")),
        project_id=anchor_id,
        title=title,
    )
    return str(report.id)


def save_batch_from_job(session: Session, job: BackgroundJob) -> list[str]:
    """Обратная совместимость: один объединённый отчёт → список из одного id."""
    rid = save_combined_from_job(session, job)
    return [rid] if rid else []
