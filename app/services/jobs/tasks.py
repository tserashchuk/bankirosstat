"""Функции-задачи, исполняемые ARQ-воркером.

Каждая функция принимает `ctx` (контекст ARQ) и `job_id` (UUID нашей таблицы
`background_jobs`) — статус и результат пишутся в БД через `app.services.jobs.store`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from sqlmodel import Session

from app.database import engine
from app.models import InternalProject, ReportHistory, SyncMeeting
from app.repositories import get_project_with_sources
from app.services.jobs.store import (
    JobCancelledError,
    finish_job_error,
    finish_job_ok,
    get_job,
    mark_running,
    raise_if_job_cancelled,
    update_progress,
)

logger = logging.getLogger(__name__)


async def report_generate(ctx: dict[str, Any], job_id: str) -> None:
    """Полный цикл генерации отчёта (один проект или весь портфель)."""
    from app.services.report_jobs import run_report_job

    await run_report_job(job_id)


async def roadmap_save(ctx: dict[str, Any], job_id: str) -> None:
    """Скачивает roadmap из Google и сохраняет таблицу в БД."""
    from app.services.roadmap_normalize import fetch_and_save_roadmap

    job = get_job(job_id)
    if not job:
        logger.error("roadmap_save: job %s not found", job_id)
        return

    payload = job.payload or {}
    raw_pid = payload.get("project_id")
    if not raw_pid:
        finish_job_error(job_id, "project_id отсутствует в payload")
        return
    try:
        project_id = uuid.UUID(str(raw_pid))
    except ValueError:
        finish_job_error(job_id, "Некорректный project_id")
        return

    mark_running(job_id, "Скачиваем roadmap из Google...")

    try:
        raise_if_job_cancelled(job_id)
        with Session(engine) as session:
            project, sources = get_project_with_sources(session, project_id)
            if not project:
                raise ValueError("Проект не найден")
            project_name = project.name
            update_progress(job_id, "Сохраняем строки в БД...")
            result = await fetch_and_save_roadmap(session, project_id, sources)

        if not result.get("rows"):
            finish_job_error(
                job_id,
                "Нет данных для сохранения. Добавьте Google Sheet и проверьте доступ.",
            )
            return

        finish_job_ok(
            job_id,
            {
                "project_id": str(project_id),
                "project_name": project_name,
                **result,
            },
            progress=f"Сохранено строк: {len(result.get('rows') or [])}",
        )
    except JobCancelledError:
        logger.info("roadmap_save cancelled job_id=%s", job_id)
    except Exception as exc:
        logger.exception("roadmap_save failed job_id=%s", job_id)
        finish_job_error(job_id, str(exc))


async def sync_attach(ctx: dict[str, Any], job_id: str) -> None:
    """Прикрепляет к отчёту расшифровку синка и извлекает задачи через ИИ."""
    from app.services.sync_meetings import attach_sync_to_report

    job = get_job(job_id)
    if not job:
        logger.error("sync_attach: job %s not found", job_id)
        return

    payload = job.payload or {}
    try:
        report_id = uuid.UUID(str(payload.get("report_id")))
    except (TypeError, ValueError):
        finish_job_error(job_id, "Некорректный report_id")
        return

    title = str(payload.get("title") or "")
    transcript = str(payload.get("transcript") or "")
    model_key = str(payload.get("model_key") or "")
    meeting_at_raw = payload.get("meeting_at")
    meeting_at: datetime | None = None
    if meeting_at_raw:
        try:
            meeting_at = datetime.fromisoformat(str(meeting_at_raw).replace("Z", "+00:00"))
        except ValueError:
            meeting_at = None

    mark_running(job_id, "Извлекаем задачи из расшифровки...")

    try:
        raise_if_job_cancelled(job_id)
        with Session(engine) as session:
            meeting, tasks = await attach_sync_to_report(
                session,
                report_id,
                title,
                transcript,
                model_key,
                meeting_at,
            )
            meeting_id = meeting.id
            project = session.get(InternalProject, meeting.project_id)
            project_name = project.name if project else ""
            tasks_count = len(tasks)

        finish_job_ok(
            job_id,
            {
                "report_id": str(report_id),
                "meeting_id": str(meeting_id),
                "project_name": project_name,
                "tasks_count": tasks_count,
            },
            progress=f"Извлечено задач: {tasks_count}",
        )
    except JobCancelledError:
        logger.info("sync_attach cancelled job_id=%s", job_id)
    except ValueError as exc:
        finish_job_error(job_id, str(exc))
    except Exception as exc:
        logger.exception("sync_attach failed job_id=%s", job_id)
        finish_job_error(job_id, str(exc))


async def sync_reextract(ctx: dict[str, Any], job_id: str) -> None:
    """Перегенерирует задачи для уже существующей встречи."""
    from app.services.sync_meetings import (
        extract_tasks_with_llm,
        save_tasks_for_meeting,
    )

    job = get_job(job_id)
    if not job:
        logger.error("sync_reextract: job %s not found", job_id)
        return

    payload = job.payload or {}
    try:
        meeting_id = uuid.UUID(str(payload.get("meeting_id")))
    except (TypeError, ValueError):
        finish_job_error(job_id, "Некорректный meeting_id")
        return
    model_key = str(payload.get("model_key") or "")

    mark_running(job_id, "Перегенерируем задачи через ИИ...")

    try:
        raise_if_job_cancelled(job_id)
        with Session(engine) as session:
            meeting = session.get(SyncMeeting, meeting_id)
            if not meeting:
                raise ValueError("Встреча не найдена")
            project = session.get(InternalProject, meeting.project_id)
            if not project:
                raise ValueError("Проект не найден")

            items = await extract_tasks_with_llm(
                model_key,
                project.name,
                meeting.title,
                meeting.transcript,
            )
            meeting.model_used = model_key
            session.add(meeting)
            session.commit()
            tasks = save_tasks_for_meeting(session, meeting, items)
            project_name = project.name

        finish_job_ok(
            job_id,
            {
                "meeting_id": str(meeting_id),
                "project_name": project_name,
                "tasks_count": len(tasks),
            },
            progress=f"Извлечено задач: {len(tasks)}",
        )
    except JobCancelledError:
        logger.info("sync_reextract cancelled job_id=%s", job_id)
    except ValueError as exc:
        finish_job_error(job_id, str(exc))
    except Exception as exc:
        logger.exception("sync_reextract failed job_id=%s", job_id)
        finish_job_error(job_id, str(exc))


JOB_FUNCTIONS = [
    report_generate,
    roadmap_save,
    sync_attach,
    sync_reextract,
]
