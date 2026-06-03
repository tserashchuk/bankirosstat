from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from app.models import InternalProject, ReportHistory, SyncMeeting, SyncTask, TaskPriority, TaskStatus
from app.services.report_history import report_display_name
from app.services.llm.generator import complete_text
from app.services.llm.sync_tasks_prompt import SYNC_TASKS_SYSTEM, build_sync_tasks_user_prompt

_MAX_TRANSCRIPT = 80_000

logger = logging.getLogger(__name__)


def _parse_tasks_json(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fence:
        text = fence.group(1).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Ожидался JSON-массив задач")
    return data


def _normalize_priority(value: Any) -> TaskPriority:
    v = str(value or "medium").lower()
    if v == "high":
        return TaskPriority.HIGH
    if v == "low":
        return TaskPriority.LOW
    return TaskPriority.MEDIUM


def list_meetings_for_report(session: Session, report_id: uuid.UUID) -> list[SyncMeeting]:
    meetings = list(
        session.exec(select(SyncMeeting).where(SyncMeeting.report_id == report_id)).all()
    )
    meetings.sort(
        key=lambda m: (
            m.meeting_at or datetime.min,
            m.created_at,
        ),
        reverse=True,
    )
    return meetings


def get_meeting_for_report(session: Session, report_id: uuid.UUID) -> SyncMeeting | None:
    """Последняя встреча по отчёту (для обратной совместимости)."""
    meetings = list_meetings_for_report(session, report_id)
    return meetings[0] if meetings else None


def get_all_tasks_for_report(session: Session, report_id: uuid.UUID) -> list[SyncTask]:
    tasks: list[SyncTask] = []
    for meeting in list_meetings_for_report(session, report_id):
        tasks.extend(get_tasks_for_meeting(session, meeting.id))
    return tasks


def get_tasks_for_meeting(session: Session, meeting_id: uuid.UUID) -> list[SyncTask]:
    return list(
        session.exec(
            select(SyncTask)
            .where(SyncTask.meeting_id == meeting_id)
            .order_by(SyncTask.sort_order, SyncTask.created_at)
        ).all()
    )


async def extract_tasks_with_llm(
    model_key: str,
    project_name: str,
    meeting_title: str,
    transcript: str,
) -> list[dict[str, Any]]:
    if len(transcript) > _MAX_TRANSCRIPT:
        transcript = transcript[:_MAX_TRANSCRIPT] + "\n… (обрезано)"
    user_prompt = build_sync_tasks_user_prompt(project_name, meeting_title, transcript)
    raw = await complete_text(model_key, SYNC_TASKS_SYSTEM, user_prompt)
    try:
        return _parse_tasks_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.exception(
            "Failed to parse sync tasks JSON project=%s model=%s",
            project_name,
            model_key,
        )
        raise ValueError(f"ИИ вернул невалидный JSON задач: {exc}") from exc


def save_tasks_for_meeting(
    session: Session,
    meeting: SyncMeeting,
    items: list[dict[str, Any]],
    *,
    replace_existing: bool = True,
) -> list[SyncTask]:
    if replace_existing:
        old = session.exec(select(SyncTask).where(SyncTask.meeting_id == meeting.id)).all()
        for t in old:
            session.delete(t)

    tasks: list[SyncTask] = []
    for idx, item in enumerate(items):
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        due = item.get("due_date")
        due_str = str(due).strip()[:10] if due else None
        task = SyncTask(
            meeting_id=meeting.id,
            project_id=meeting.project_id,
            title=title[:500],
            description=(str(item.get("description")).strip()[:2000] if item.get("description") else None),
            assignee=(str(item.get("assignee")).strip()[:255] if item.get("assignee") else None),
            due_date=due_str,
            priority=_normalize_priority(item.get("priority")),
            status=TaskStatus.OPEN,
            sort_order=idx,
        )
        session.add(task)
        tasks.append(task)

    meeting.extracted_at = datetime.utcnow()
    meeting.model_used = meeting.model_used or "unknown"
    session.add(meeting)
    session.commit()
    for t in tasks:
        session.refresh(t)
    return tasks


async def attach_sync_to_report(
    session: Session,
    report_id: uuid.UUID,
    title: str,
    transcript: str,
    model_key: str,
    meeting_at: datetime | None = None,
) -> tuple[SyncMeeting, list[SyncTask]]:
    report = session.get(ReportHistory, report_id)
    if not report:
        raise ValueError("Отчёт не найден")
    project = (
        session.get(InternalProject, report.project_id) if report.project_id else None
    )
    project_name = report_display_name(report, project)
    if not report.project_id and not project_name:
        raise ValueError("Проект не найден")

    transcript = transcript.strip()
    if not transcript:
        raise ValueError("Вставьте расшифровку или заметки со встречи")

    if not report.project_id:
        raise ValueError("Для синка нужен отчёт с привязкой к проекту")

    meeting = SyncMeeting(
        report_id=report_id,
        project_id=report.project_id,
        title=title.strip() or "Созвон / синк",
        transcript=transcript,
        meeting_at=meeting_at,
        model_used=model_key,
    )
    session.add(meeting)
    session.commit()
    session.refresh(meeting)

    items = await extract_tasks_with_llm(
        model_key, project_name, meeting.title, transcript
    )
    tasks = save_tasks_for_meeting(session, meeting, items)
    return meeting, tasks
