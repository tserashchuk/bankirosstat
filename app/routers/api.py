from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session, select

from app.crypto import encrypt_credentials
from app.config import get_settings
from app.database import engine, get_session
from app.models import (
    BackgroundJobStatus,
    BackgroundJobType,
    Employee,
    InternalProject,
    ProjectEmployee,
    ProjectSource,
    RagStatus,
    ReportHistory,
    SourceType,
    SyncMeeting,
    SyncTask,
    TaskStatus,
)
from app.services.collectors.employee_yandex import (
    build_yandex_calendar_credentials,
    build_yandex_mail_credentials,
    test_employee_yandex_connections,
)
from app.services.employee_credentials import rebind_yandex_email
from app.services.collectors.calendar import test_calendar_connection
from app.services.collectors.email_imap import test_email_connection
from app.services.collectors.google_doc import test_google_doc_connection
from app.services.collectors.google_sheet import test_google_sheet_connection
from app.services.collectors.youtrack import test_youtrack_connection
from app.services.jobs import (
    create_job,
    get_job as get_bg_job,
    list_active_jobs,
    serialize_job,
)
from app.services.jobs.queue import enqueue
from app.services.llm.prompts import normalize_report_format
from app.repositories import delete_report_with_relations, get_project_with_sources
from app.services.roadmap_normalize import fetch_roadmap_from_google
from app.services.roadmap_repository import get_project_roadmap, roadmap_to_api_payload
from app.services.report_history import report_display_name
from app.services.report_jobs import (
    save_combined_from_job,
    save_report_to_history,
)
from app.services.source_creds import normalize_credentials, prepare_email_creds
from app.services.sync_meetings import (
    get_tasks_for_meeting,
    list_meetings_for_report,
)
from app.services.gamma import (
    create_generation as gamma_create_generation,
    create_generation_from_template as gamma_create_from_template,
    get_generation_status as gamma_get_generation_status,
)
from app.services.gamma_roadmap import build_gamma_prompt_for_report

router = APIRouter(prefix="/api")
logger = logging.getLogger(__name__)


class GenerateRequest(BaseModel):
    model_key: str
    report_format: str = "brief_progress"
    project_id: uuid.UUID | None = None


class SaveReportRequest(BaseModel):
    project_id: uuid.UUID
    model_key: str
    text: str
    snapshot: dict[str, Any] | None = None
    rag_status: str = "AMBER"
    title: str | None = None


class ReportPatchRequest(BaseModel):
    text: str


class ReportSyncAttachRequest(BaseModel):
    title: str = ""
    transcript: str
    model_key: str
    meeting_at: str | None = None


class SyncMeetingExtractRequest(BaseModel):
    model_key: str


class SyncTaskPatchRequest(BaseModel):
    status: str


class ProjectUpdateRequest(BaseModel):
    name: str
    description: str = ""


class GammaGenerateRequest(BaseModel):
    num_cards: int = 10
    export_as: str = "pptx"


class GammaResultAttachRequest(BaseModel):
    generation_id: str
    gamma_url: str | None = None
    export_url: str | None = None


@router.post("/projects")
def create_project(
    name: str = Form(...),
    description: str = Form(""),
    session: Session = Depends(get_session),
):
    project = InternalProject(name=name.strip(), description=description.strip() or None)
    session.add(project)
    session.commit()
    session.refresh(project)
    return {"id": str(project.id), "name": project.name}


@router.patch("/projects/{project_id}")
def update_project(
    project_id: uuid.UUID,
    req: ProjectUpdateRequest,
    session: Session = Depends(get_session),
):
    project = session.get(InternalProject, project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Название не может быть пустым")
    project.name = name[:255]
    desc = req.description.strip()
    project.description = desc[:5000] if desc else None
    session.add(project)
    session.commit()
    session.refresh(project)
    return {
        "id": str(project.id),
        "name": project.name,
        "description": project.description or "",
    }


@router.get("/projects/{project_id}")
def get_project(project_id: uuid.UUID, session: Session = Depends(get_session)):
    project = session.get(InternalProject, project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    return {
        "id": str(project.id),
        "name": project.name,
        "description": project.description or "",
    }


@router.get("/projects/{project_id}/roadmap")
def get_saved_project_roadmap(
    project_id: uuid.UUID,
    session: Session = Depends(get_session),
):
    """Сохранённый roadmap из базы (все колонки и строки)."""
    project = session.get(InternalProject, project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    roadmap = get_project_roadmap(session, project_id)
    if not roadmap:
        return {
            "project_id": str(project_id),
            "project_name": project.name,
            "columns": [],
            "rows": [],
            "saved": False,
            "saved_at": None,
            "meta": {"from_db": False, "rows_returned": 0},
        }
    payload = roadmap_to_api_payload(roadmap)
    return {
        "project_id": str(project_id),
        "project_name": project.name,
        **payload,
    }


@router.get("/projects/{project_id}/roadmap-table")
async def preview_project_roadmap_table(
    project_id: uuid.UUID,
    session: Session = Depends(get_session),
):
    """Скачивает Google Sheet/Doc — все колонки и строки (превью, без сохранения)."""
    project, sources = get_project_with_sources(session, project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    table = await fetch_roadmap_from_google(sources)
    return {
        "project_id": str(project_id),
        "project_name": project.name,
        **table,
    }


@router.post("/projects/{project_id}/roadmap/save")
async def save_project_roadmap(
    project_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
):
    """Импорт из Google и сохранение полной таблицы в БД — асинхронно через очередь."""
    project = session.get(InternalProject, project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")

    pool = request.app.state.arq_pool
    if pool is None:
        raise HTTPException(503, "Очередь задач недоступна (Redis/worker не запущены)")

    job = create_job(
        BackgroundJobType.ROADMAP_SAVE,
        title=f"Roadmap: {project.name}",
        project_id=project_id,
        payload={"project_id": str(project_id)},
        progress="В очереди...",
    )
    await enqueue(pool, "roadmap_save", job.id)
    return {"job_id": str(job.id), "status": job.status.value}


@router.delete("/projects/{project_id}")
def delete_project(project_id: uuid.UUID, session: Session = Depends(get_session)):
    project = session.get(InternalProject, project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    session.delete(project)
    session.commit()
    return {"ok": True}


@router.post("/projects/{project_id}/sources")
def add_source(
    project_id: uuid.UUID,
    source_type: str = Form(...),
    label: str = Form(""),
    credentials_json: str = Form(...),
    session: Session = Depends(get_session),
):
    project = session.get(InternalProject, project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    try:
        creds = normalize_credentials(json.loads(credentials_json))
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Невалидный JSON: {e}") from e

    try:
        st = SourceType(source_type)
    except ValueError as e:
        raise HTTPException(400, f"Неизвестный тип источника: {source_type}") from e

    if st == SourceType.EMAIL:
        creds = prepare_email_creds(creds)

    source = ProjectSource(
        project_id=project_id,
        source_type=st,
        label=label.strip() or None,
        credentials_encrypted=encrypt_credentials(creds),
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return {"id": str(source.id), "source_type": source.source_type.value}


@router.delete("/sources/{source_id}")
def delete_source(source_id: uuid.UUID, session: Session = Depends(get_session)):
    source = session.get(ProjectSource, source_id)
    if not source:
        raise HTTPException(404, "Источник не найден")
    session.delete(source)
    session.commit()
    return {"ok": True}


@router.post("/sources/{source_id}/test")
async def test_source(source_id: uuid.UUID, session: Session = Depends(get_session)):
    source = session.get(ProjectSource, source_id)
    if not source:
        raise HTTPException(404, "Источник не найден")

    enc = source.credentials_encrypted
    if source.source_type == SourceType.YOUTRACK:
        ok, msg = await test_youtrack_connection(enc)
    elif source.source_type == SourceType.EMAIL:
        ok, msg = test_email_connection(enc)
    elif source.source_type == SourceType.CALENDAR:
        ok, msg = test_calendar_connection(enc)
    elif source.source_type == SourceType.GOOGLE_DOC:
        ok, msg = await test_google_doc_connection(enc)
    elif source.source_type == SourceType.GOOGLE_SHEET:
        ok, msg = await test_google_sheet_connection(enc)
    else:
        ok, msg = False, "Неизвестный тип"

    return {"ok": ok, "message": msg}


@router.post("/employees")
def create_employee(
    full_name: str = Form(""),
    yandex_email: str = Form(""),
    mail_password: str = Form(""),
    calendar_password: str = Form(""),
    app_password: str = Form(""),
    session: Session = Depends(get_session),
):
    name = full_name.strip()
    email = yandex_email.strip().lower()
    if not name and not email:
        raise HTTPException(400, "Укажите ФИО или email Yandex")
    if email and "@" not in email:
        raise HTTPException(400, "Укажите корректный email Yandex")

    pw_mail = mail_password.strip() or app_password.strip()
    pw_cal = calendar_password.strip()
    if (pw_mail or pw_cal) and not email:
        raise HTTPException(400, "Для паролей нужен email Yandex")

    imap_enc = (
        encrypt_credentials(build_yandex_mail_credentials(email, pw_mail)) if pw_mail else None
    )
    cal_enc = (
        encrypt_credentials(build_yandex_calendar_credentials(email, pw_cal))
        if pw_cal
        else None
    )
    employee = Employee(
        full_name=name or email.split("@")[0],
        yandex_email=email,
        imap_password_encrypted=imap_enc,
        calendar_password_encrypted=cal_enc,
    )
    session.add(employee)
    session.commit()
    session.refresh(employee)
    return {"id": str(employee.id), "full_name": employee.full_name}


@router.put("/employees/{employee_id}")
def update_employee(
    employee_id: uuid.UUID,
    full_name: str = Form(""),
    yandex_email: str = Form(""),
    mail_password: str = Form(""),
    calendar_password: str = Form(""),
    app_password: str = Form(""),
    session: Session = Depends(get_session),
):
    employee = session.get(Employee, employee_id)
    if not employee:
        raise HTTPException(404, "Сотрудник не найден")
    name = full_name.strip()
    email = yandex_email.strip().lower()
    if not name and not email:
        raise HTTPException(400, "Укажите ФИО или email Yandex")
    if email and "@" not in email:
        raise HTTPException(400, "Укажите корректный email Yandex")
    if name:
        employee.full_name = name
    old_email = (employee.yandex_email or "").strip().lower()
    employee.yandex_email = email

    pw_mail = mail_password.strip() or app_password.strip()
    pw_cal = calendar_password.strip()
    if (pw_mail or pw_cal) and not email:
        raise HTTPException(400, "Для паролей нужен email Yandex")
    if pw_mail:
        employee.imap_password_encrypted = encrypt_credentials(
            build_yandex_mail_credentials(email, pw_mail)
        )
    elif email and email != old_email and employee.imap_password_encrypted:
        employee.imap_password_encrypted = rebind_yandex_email(
            employee.imap_password_encrypted, email
        )

    if pw_cal:
        employee.calendar_password_encrypted = encrypt_credentials(
            build_yandex_calendar_credentials(email, pw_cal)
        )
    elif email and email != old_email and employee.calendar_password_encrypted:
        employee.calendar_password_encrypted = rebind_yandex_email(
            employee.calendar_password_encrypted, email
        )

    session.add(employee)
    session.commit()
    return {"ok": True}


@router.delete("/employees/{employee_id}")
def delete_employee(employee_id: uuid.UUID, session: Session = Depends(get_session)):
    employee = session.get(Employee, employee_id)
    if not employee:
        raise HTTPException(404, "Сотрудник не найден")
    for link in session.exec(
        select(ProjectEmployee).where(ProjectEmployee.employee_id == employee_id)
    ).all():
        session.delete(link)
    session.delete(employee)
    session.commit()
    return {"ok": True}


@router.post("/employees/{employee_id}/test-mail")
def test_employee_mail(employee_id: uuid.UUID, session: Session = Depends(get_session)):
    employee = session.get(Employee, employee_id)
    if not employee:
        raise HTTPException(404, "Сотрудник не найден")
    ok, msg = test_employee_yandex_connections(employee)
    return {"ok": ok, "message": msg}


@router.post("/projects/{project_id}/employees/{employee_id}")
def assign_employee_to_project(
    project_id: uuid.UUID,
    employee_id: uuid.UUID,
    session: Session = Depends(get_session),
):
    if not session.get(InternalProject, project_id):
        raise HTTPException(404, "Проект не найден")
    if not session.get(Employee, employee_id):
        raise HTTPException(404, "Сотрудник не найден")
    existing = session.exec(
        select(ProjectEmployee).where(
            ProjectEmployee.project_id == project_id,
            ProjectEmployee.employee_id == employee_id,
        )
    ).first()
    if existing:
        return {"ok": True, "already": True}
    session.add(ProjectEmployee(project_id=project_id, employee_id=employee_id))
    session.commit()
    return {"ok": True}


@router.delete("/projects/{project_id}/employees/{employee_id}")
def unassign_employee_from_project(
    project_id: uuid.UUID,
    employee_id: uuid.UUID,
    session: Session = Depends(get_session),
):
    link = session.exec(
        select(ProjectEmployee).where(
            ProjectEmployee.project_id == project_id,
            ProjectEmployee.employee_id == employee_id,
        )
    ).first()
    if not link:
        raise HTTPException(404, "Сотрудник не привязан к проекту")
    session.delete(link)
    session.commit()
    return {"ok": True}


@router.post("/reports/generate")
async def generate(req: GenerateRequest, request: Request):
    fmt = normalize_report_format(req.report_format)
    pool = request.app.state.arq_pool
    if pool is None:
        raise HTTPException(503, "Очередь задач недоступна (Redis/worker не запущены)")

    title = "Отчёт по портфелю"
    if req.project_id:
        with Session(engine) as s:
            project = s.get(InternalProject, req.project_id)
            if project:
                title = f"Отчёт: {project.name}"

    job = create_job(
        BackgroundJobType.REPORT_GENERATE,
        title=title,
        project_id=req.project_id,
        payload={
            "model_key": req.model_key,
            "report_format": fmt,
            "project_id": str(req.project_id) if req.project_id else None,
        },
        progress="В очереди...",
    )
    await enqueue(pool, "report_generate", job.id)
    return {
        "job_id": str(job.id),
        "report_format": fmt,
        "all_projects": req.project_id is None,
    }


def _report_job_payload(job) -> dict[str, Any]:
    """Совместимый со старым фронтом формат ответа для отчёта."""
    payload = serialize_job(job, include_result=True)
    result = job.result or {}
    project_reports_full = result.get("project_reports") or []
    return {
        "job_id": payload["id"],
        "title": job.title,
        "status": payload["status"],
        "progress": payload["progress"],
        "error": payload["error"],
        "report_text": result.get("report_text"),
        "rag_status": result.get("rag_status"),
        "snapshot": result.get("snapshot"),
        "report_format": result.get("report_format") or (job.payload or {}).get("report_format"),
        "portfolio_summary": result.get("portfolio_summary"),
        "project_reports": [
            {
                "project_id": pr.get("project_id"),
                "project_name": pr.get("project_name"),
                "rag_status": pr.get("rag_status"),
                "error": pr.get("error"),
            }
            for pr in project_reports_full
        ],
    }


@router.get("/reports/jobs/{job_id}")
def job_status(job_id: str):
    job = get_bg_job(job_id)
    if not job or job.type != BackgroundJobType.REPORT_GENERATE:
        raise HTTPException(404, "Задача не найдена")
    return _report_job_payload(job)


@router.get("/jobs/active")
def jobs_active():
    """Активные + недавно завершённые задачи для глобального виджета."""
    jobs = list_active_jobs(limit=20)
    return {
        "jobs": [serialize_job(j, include_result=False) for j in jobs],
    }


@router.get("/jobs/{job_id}")
def universal_job_status(job_id: str):
    job = get_bg_job(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена")
    return serialize_job(job, include_result=True)


@router.post("/reports/save")
def save_report(req: SaveReportRequest, session: Session = Depends(get_session)):
    project = session.get(InternalProject, req.project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")
    try:
        rag = RagStatus(req.rag_status.upper())
    except ValueError:
        rag = RagStatus.AMBER
    report = save_report_to_history(
        session,
        req.model_key,
        req.snapshot or {},
        req.text,
        rag,
        project_id=req.project_id,
        title=req.title,
    )
    return {"id": str(report.id)}


@router.post("/reports/save-batch/{job_id}")
def save_reports_batch(job_id: str, session: Session = Depends(get_session)):
    """Сохраняет один объединённый отчёт из фоновой задачи."""
    job = get_bg_job(job_id)
    if not job or job.type != BackgroundJobType.REPORT_GENERATE:
        raise HTTPException(404, "Задача не найдена")
    if job.status != BackgroundJobStatus.DONE:
        raise HTTPException(404, "Готовый отчёт не найден — сначала сгенерируйте")
    report_text = (job.result or {}).get("report_text") or ""
    if not report_text.strip():
        raise HTTPException(400, "Нет текста отчёта для сохранения")
    report_id = save_combined_from_job(session, job)
    if not report_id:
        raise HTTPException(400, "Не удалось сохранить отчёт")
    return {"saved": 1, "id": report_id, "ids": [report_id]}


@router.patch("/reports/{report_id}")
def patch_report(
    report_id: uuid.UUID,
    req: ReportPatchRequest,
    session: Session = Depends(get_session),
):
    report = session.get(ReportHistory, report_id)
    if not report:
        raise HTTPException(404, "Отчёт не найден")
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "Текст отчёта не может быть пустым")
    report.generated_text = text
    session.add(report)
    session.commit()
    session.refresh(report)
    project = session.get(InternalProject, report.project_id) if report.project_id else None
    return {
        "id": str(report.id),
        "generated_text": report.generated_text,
        "display_name": report_display_name(report, project),
    }


@router.get("/reports/{report_id}")
def get_report(report_id: uuid.UUID, session: Session = Depends(get_session)):
    report = session.get(ReportHistory, report_id)
    if not report:
        raise HTTPException(404, "Отчёт не найден")
    project = session.get(InternalProject, report.project_id) if report.project_id else None
    payload: dict[str, Any] = {
        "id": str(report.id),
        "generated_text": report.generated_text,
        "model_used": report.model_used,
        "rag_status": report.rag_status.value,
        "created_at": report.created_at.isoformat(),
        "title": report.title,
        "project_name": report_display_name(report, project),
        "is_portfolio": bool((report.raw_data_snapshot or {}).get("portfolio")),
        "gamma_generation_id": report.gamma_generation_id,
        "gamma_url": report.gamma_url,
        "gamma_export_url": report.gamma_export_url,
        "sync_meetings": [],
    }
    display = report_display_name(report, project)
    for meeting in list_meetings_for_report(session, report_id):
        tasks = get_tasks_for_meeting(session, meeting.id)
        payload["sync_meetings"].append(_serialize_sync_meeting(meeting, display, tasks))
    return payload


@router.post("/reports/{report_id}/gamma")
async def generate_gamma_presentation(
    report_id: uuid.UUID,
    req: GammaGenerateRequest,
    session: Session = Depends(get_session),
):
    settings = get_settings()
    if not settings.gamma_api_key:
        raise HTTPException(400, "GAMMA_API_KEY не задан в .env")

    report = session.get(ReportHistory, report_id)
    if not report:
        raise HTTPException(404, "Отчёт не найден")
    project = session.get(InternalProject, report.project_id) if report.project_id else None
    display_name = report_display_name(report, project)
    text = (report.generated_text or "").strip()
    if not text:
        raise HTTPException(400, "У отчёта пустой текст")

    snap = report.raw_data_snapshot or {}
    if snap.get("portfolio"):
        n = len(snap.get("projects") or [])
        title = f"Портфель ({n} проектов) — отчётная презентация"
    else:
        title = f"{display_name} — отчётная презентация"
    template_id = (settings.gamma_template_id or "").strip()
    sync_tasks_block = _build_gamma_sync_tasks_block(session, report_id)
    gamma_body = build_gamma_prompt_for_report(
        session,
        report,
        sync_tasks_block=sync_tasks_block,
        from_template=bool(template_id),
    )
    try:
        if template_id:
            generation_id = await gamma_create_from_template(
                gamma_id=template_id,
                prompt=gamma_body,
                title=title,
                export_as=req.export_as,
            )
        else:
            generation_id = await gamma_create_generation(
                input_text=gamma_body,
                title=title,
                num_cards=req.num_cards,
                export_as=req.export_as,
                language=settings.gamma_language,
            )
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    report.gamma_generation_id = generation_id
    session.add(report)
    session.commit()
    return {"generation_id": generation_id}


@router.get("/gamma/generations/{generation_id}")
async def get_gamma_generation(generation_id: str):
    settings = get_settings()
    if not settings.gamma_api_key:
        raise HTTPException(400, "GAMMA_API_KEY не задан в .env")
    try:
        data = await gamma_get_generation_status(generation_id)
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    return data


@router.patch("/reports/{report_id}/gamma")
def attach_gamma_links(
    report_id: uuid.UUID,
    req: GammaResultAttachRequest,
    session: Session = Depends(get_session),
):
    report = session.get(ReportHistory, report_id)
    if not report:
        raise HTTPException(404, "Отчёт не найден")
    report.gamma_generation_id = req.generation_id.strip()[:128]
    report.gamma_url = (req.gamma_url or "").strip() or None
    report.gamma_export_url = (req.export_url or "").strip() or None
    session.add(report)
    session.commit()
    return {
        "ok": True,
        "gamma_generation_id": report.gamma_generation_id,
        "gamma_url": report.gamma_url,
        "gamma_export_url": report.gamma_export_url,
    }


@router.delete("/reports/{report_id}")
def delete_report(report_id: uuid.UUID, session: Session = Depends(get_session)):
    """Удаляет отчёт и связанные расшифровку / задачи синка."""
    if not delete_report_with_relations(session, report_id):
        raise HTTPException(404, "Отчёт не найден")
    return {"ok": True}


def _serialize_sync_task(task: SyncTask) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "title": task.title,
        "description": task.description,
        "assignee": task.assignee,
        "due_date": task.due_date,
        "priority": task.priority.value,
        "status": task.status.value,
        "sort_order": task.sort_order,
    }


def _serialize_sync_meeting(meeting: SyncMeeting, project_name: str, tasks: list[SyncTask]) -> dict[str, Any]:
    return {
        "id": str(meeting.id),
        "report_id": str(meeting.report_id) if meeting.report_id else None,
        "project_id": str(meeting.project_id),
        "project_name": project_name,
        "title": meeting.title,
        "transcript": meeting.transcript,
        "meeting_at": meeting.meeting_at.isoformat() if meeting.meeting_at else None,
        "model_used": meeting.model_used,
        "extracted_at": meeting.extracted_at.isoformat() if meeting.extracted_at else None,
        "created_at": meeting.created_at.isoformat(),
        "tasks": [_serialize_sync_task(t) for t in tasks],
    }


def _parse_meeting_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, "Некорректная дата встречи") from exc


def _build_gamma_sync_tasks_block(session: Session, report_id: uuid.UUID) -> str:
    meetings = list_meetings_for_report(session, report_id)
    if not meetings:
        return ""

    lines = [
        "---",
        "## Задачи синков (если есть)",
        "",
        (
            "Используй задачи из синков как подтверждение фактического прогресса и как вход для слайдов "
            "«Результаты», «План vs факт» и «Фокус». Не выдумывай задачи вне списка ниже."
        ),
    ]
    has_tasks = False
    for idx, meeting in enumerate(meetings, start=1):
        tasks = get_tasks_for_meeting(session, meeting.id)
        if not tasks:
            continue
        has_tasks = True
        meeting_title = (meeting.title or "Синк").strip()
        meeting_date = meeting.meeting_at.strftime("%d.%m.%Y") if meeting.meeting_at else "без даты"
        lines.append("")
        lines.append(f"### Синк {idx}: {meeting_title} ({meeting_date})")
        for task_idx, task in enumerate(tasks, start=1):
            title = (task.title or "").strip()
            if not title:
                continue
            status = task.status.value
            assignee = (task.assignee or "").strip()
            due_date = (task.due_date or "").strip()
            parts = [f"{task_idx}. {title}", f"статус: {status}"]
            if assignee:
                parts.append(f"ответственный: {assignee}")
            if due_date:
                parts.append(f"срок: {due_date}")
            lines.append(" — ".join(parts))

    if not has_tasks:
        return ""
    return "\n".join(lines)


@router.get("/reports/{report_id}/sync")
def get_report_sync(report_id: uuid.UUID, session: Session = Depends(get_session)):
    report = session.get(ReportHistory, report_id)
    if not report:
        raise HTTPException(404, "Отчёт не найден")
    project = session.get(InternalProject, report.project_id) if report.project_id else None
    display = report_display_name(report, project)
    return [
        _serialize_sync_meeting(m, display, get_tasks_for_meeting(session, m.id))
        for m in list_meetings_for_report(session, report_id)
    ]


@router.post("/reports/{report_id}/sync")
async def attach_report_sync(
    report_id: uuid.UUID,
    req: ReportSyncAttachRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """Добавляет новую встречу к отчёту и ставит в очередь извлечение задач. Возвращает job_id."""
    report = session.get(ReportHistory, report_id)
    if not report:
        raise HTTPException(404, "Отчёт не найден")
    transcript = (req.transcript or "").strip()
    if not transcript:
        raise HTTPException(400, "Вставьте расшифровку или заметки со встречи")
    meeting_at = _parse_meeting_at(req.meeting_at)

    pool = request.app.state.arq_pool
    if pool is None:
        raise HTTPException(503, "Очередь задач недоступна (Redis/worker не запущены)")

    project = session.get(InternalProject, report.project_id)
    title = req.title.strip() or "Созвон / синк"
    job = create_job(
        BackgroundJobType.SYNC_ATTACH,
        title=f"Синк: {project.name if project else title}",
        project_id=report.project_id,
        payload={
            "report_id": str(report_id),
            "title": title,
            "transcript": transcript,
            "model_key": req.model_key,
            "meeting_at": meeting_at.isoformat() if meeting_at else None,
        },
        progress="В очереди...",
    )
    await enqueue(pool, "sync_attach", job.id)
    return {"job_id": str(job.id), "status": job.status.value}


@router.delete("/reports/{report_id}/sync")
def delete_all_report_sync(report_id: uuid.UUID, session: Session = Depends(get_session)):
    """Удаляет все встречи и задачи синка, привязанные к отчёту."""
    meetings = list_meetings_for_report(session, report_id)
    if not meetings:
        raise HTTPException(404, "Расшифровки не прикреплены")
    for meeting in meetings:
        session.delete(meeting)
    session.commit()
    return {"ok": True, "deleted": len(meetings)}


@router.post("/sync-meetings/{meeting_id}/extract")
async def reextract_sync_tasks(
    meeting_id: uuid.UUID,
    req: SyncMeetingExtractRequest,
    request: Request,
    session: Session = Depends(get_session),
):
    """Ставит в очередь перегенерацию задач для существующей встречи."""
    meeting = session.get(SyncMeeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "Встреча не найдена")
    project = session.get(InternalProject, meeting.project_id)
    if not project:
        raise HTTPException(404, "Проект не найден")

    pool = request.app.state.arq_pool
    if pool is None:
        raise HTTPException(503, "Очередь задач недоступна (Redis/worker не запущены)")

    job = create_job(
        BackgroundJobType.SYNC_REEXTRACT,
        title=f"Перегенерация задач: {project.name}",
        project_id=meeting.project_id,
        payload={
            "meeting_id": str(meeting_id),
            "model_key": req.model_key,
        },
        progress="В очереди...",
    )
    await enqueue(pool, "sync_reextract", job.id)
    return {"job_id": str(job.id), "status": job.status.value}


@router.delete("/sync-meetings/{meeting_id}")
def delete_sync_meeting(meeting_id: uuid.UUID, session: Session = Depends(get_session)):
    meeting = session.get(SyncMeeting, meeting_id)
    if not meeting:
        raise HTTPException(404, "Встреча не найдена")
    session.delete(meeting)
    session.commit()
    return {"ok": True}


@router.patch("/sync-tasks/{task_id}")
def patch_sync_task(
    task_id: uuid.UUID,
    req: SyncTaskPatchRequest,
    session: Session = Depends(get_session),
):
    task = session.get(SyncTask, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    try:
        task.status = TaskStatus(req.status.lower())
    except ValueError as exc:
        raise HTTPException(400, "Статус: open, done, cancelled") from exc
    session.add(task)
    session.commit()
    session.refresh(task)
    return _serialize_sync_task(task)


@router.delete("/sync-tasks/{task_id}")
def delete_sync_task(task_id: uuid.UUID, session: Session = Depends(get_session)):
    task = session.get(SyncTask, task_id)
    if not task:
        raise HTTPException(404, "Задача не найдена")
    session.delete(task)
    session.commit()
    return {"ok": True}
