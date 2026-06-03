from __future__ import annotations

import uuid
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import get_settings
from app.database import get_session
from app.models import InternalProject, RagStatus, TaskPriority, TaskStatus
from app.repositories import (
    get_report_row,
    list_employees,
    list_history_report_rows,
    list_projects_with_sources,
    list_sync_task_groups,
)
from app.services.report_history import report_display_name
from app.services.llm.prompts import REPORT_FORMAT_BRIEF_PROGRESS, REPORT_FORMAT_LABELS

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

MODEL_LABELS = {
    "gemini-2.5-pro": "Gemini 2.5 Pro",
    "gemini-2.5-flash": "Gemini 2.5 Flash",
    "gemini-1.5-pro": "Gemini 2.5 Pro",
    "claude-3-5-sonnet": "Claude 3.5 Sonnet",
    "deepseek-v3": "DeepSeek V3",
    "deepseek-r1": "DeepSeek R1",
}

PRIORITY_LABELS = {
    TaskPriority.HIGH: "Высокий",
    TaskPriority.MEDIUM: "Средний",
    TaskPriority.LOW: "Низкий",
}

DEFAULT_MODEL_KEY = "gemini-2.5-pro"


def _rag_badge(status: RagStatus) -> str:
    colors = {
        RagStatus.GREEN: "bg-lime-100 text-lime-700",
        RagStatus.AMBER: "bg-amber-100 text-amber-700",
        RagStatus.RED: "bg-red-100 text-red-700",
    }
    return colors.get(status, "bg-slate-100 text-slate-700")


def _models_context(settings) -> list[dict]:
    return [
        {
            "key": k,
            "label": MODEL_LABELS[k],
            "enabled": settings.available_models.get(k, False),
        }
        for k in MODEL_LABELS
    ]


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: Session = Depends(get_session)):
    settings = get_settings()
    projects = session.exec(select(InternalProject).order_by(InternalProject.name)).all()
    report_formats = [
        {"key": key, "label": label}
        for key, label in REPORT_FORMAT_LABELS.items()
    ]
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "projects": projects,
            "models": _models_context(settings),
            "default_model": settings.resolve_default_model(DEFAULT_MODEL_KEY),
            "report_formats": report_formats,
            "default_report_format": REPORT_FORMAT_BRIEF_PROGRESS,
            "active": "dashboard",
        },
    )


@router.get("/employees", response_class=HTMLResponse)
async def employees_page(request: Request, session: Session = Depends(get_session)):
    employees = list_employees(session)
    return templates.TemplateResponse(
        request,
        "employees.html",
        {"employees": employees, "active": "employees"},
    )


@router.get("/projects", response_class=HTMLResponse)
async def projects_hub(request: Request, session: Session = Depends(get_session)):
    project_rows = list_projects_with_sources(session)
    all_employees = list_employees(session)
    projects = [
        SimpleNamespace(id=p.id, name=p.name, description=p.description, sources=s, employees=e)
        for p, s, e in project_rows
    ]
    return templates.TemplateResponse(
        request,
        "projects.html",
        {"projects": projects, "all_employees": all_employees, "active": "projects"},
    )


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, session: Session = Depends(get_session)):
    rows = []
    for report, project, meetings, tasks in list_history_report_rows(session):
        snap = report.raw_data_snapshot or {}
        rows.append(
            {
                "id": report.id,
                "created_at": report.created_at.strftime("%d.%m.%Y %H:%M"),
                "project_name": report_display_name(report, project),
                "is_portfolio": bool(snap.get("portfolio")),
                "model": MODEL_LABELS.get(report.model_used, report.model_used),
                "rag_status": report.rag_status.value,
                "rag_class": _rag_badge(report.rag_status),
                "has_sync": len(meetings) > 0,
                "meeting_count": len(meetings),
                "task_count": len(tasks),
                "gamma_url": report.gamma_url,
                "gamma_export_url": report.gamma_export_url,
            }
        )

    return templates.TemplateResponse(
        request,
        "history.html",
        {"reports": rows, "active": "history"},
    )


@router.get("/reports/{report_id}", response_class=HTMLResponse)
async def report_detail_page(
    report_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
):
    row = get_report_row(session, report_id)
    if not row:
        raise HTTPException(404, "Отчёт не найден")
    report, project, meetings, tasks = row
    settings = get_settings()
    snap = report.raw_data_snapshot or {}

    return templates.TemplateResponse(
        request,
        "report_detail.html",
        {
            "report": report,
            "report_id": str(report.id),
            "display_name": report_display_name(report, project),
            "created_at": report.created_at.strftime("%d.%m.%Y %H:%M"),
            "model_label": MODEL_LABELS.get(report.model_used, report.model_used),
            "rag_status": report.rag_status.value,
            "rag_class": _rag_badge(report.rag_status),
            "is_portfolio": bool(snap.get("portfolio")),
            "generated_text": report.generated_text,
            "has_sync": len(meetings) > 0,
            "meeting_count": len(meetings),
            "task_count": len(tasks),
            "models": _models_context(settings),
            "default_model": settings.resolve_default_model(DEFAULT_MODEL_KEY),
            "active": "history",
        },
    )


@router.get("/sync-tasks", response_class=HTMLResponse)
async def sync_tasks_page(request: Request, session: Session = Depends(get_session)):
    groups = []
    total_tasks = 0
    for g in list_sync_task_groups(session):
        meeting_blocks = []
        for block in g.meetings:
            m = block.meeting
            task_rows = []
            for t in block.tasks:
                task_rows.append(
                    {
                        "id": t.id,
                        "title": t.title,
                        "description": t.description or "",
                        "assignee": t.assignee or "",
                        "due_date": t.due_date or "",
                        "priority": t.priority.value,
                        "priority_label": PRIORITY_LABELS.get(t.priority, t.priority.value),
                        "status": t.status.value,
                        "done": t.status == TaskStatus.DONE,
                    }
                )
            meeting_date = (
                m.meeting_at.strftime("%d.%m.%Y")
                if m.meeting_at
                else m.created_at.strftime("%d.%m.%Y")
            )
            meeting_blocks.append(
                {
                    "id": m.id,
                    "title": m.title,
                    "meeting_date": meeting_date,
                    "tasks": task_rows,
                }
            )
            total_tasks += len(task_rows)
        groups.append(
            {
                "report_id": g.report_id,
                "report_name": g.report_name,
                "report_date": g.report_date,
                "rag_status": g.rag_status,
                "rag_class": _rag_badge(RagStatus(g.rag_status)),
                "meeting_count": len(meeting_blocks),
                "meetings": meeting_blocks,
            }
        )

    return templates.TemplateResponse(
        request,
        "sync_tasks.html",
        {
            "groups": groups,
            "total_tasks": total_tasks,
            "active": "sync_tasks",
        },
    )
