from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Awaitable, Callable

from sqlmodel import Session

from app.models import Employee, ProjectSource, SourceType
from app.services.collectors.employee_yandex import (
    collect_employee_yandex_calendar,
    collect_employee_yandex_mail,
)
from app.services.collectors.calendar import collect_calendar
from app.services.collectors.email_imap import collect_email
from app.services.collectors.youtrack import collect_youtrack
from app.services.collectors.collect_log import log_collect_block, log_snapshot_summary
from app.services.roadmap_normalize import enrich_snapshot_with_roadmap_table
from app.services.milestones import enrich_snapshot_with_milestones
from app.services.source_creds import (
    can_collect,
    decrypt_normalized,
    missing_labels,
    prepare_email_creds,
)

logger = logging.getLogger(__name__)

MAX_CONTEXT_CHARS = 80_000


def _truncate_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    raw = json.dumps(snapshot, ensure_ascii=False)
    if len(raw) <= MAX_CONTEXT_CHARS:
        return snapshot
    trimmed = dict(snapshot)
    for key in ("email", "employee_emails", "youtrack", "calendar"):
        if key in trimmed and isinstance(trimmed[key], list):
            while len(json.dumps(trimmed, ensure_ascii=False)) > MAX_CONTEXT_CHARS and trimmed[key]:
                trimmed[key] = trimmed[key][:-1]
    return trimmed


def _skipped_entry(src: ProjectSource, reason: str) -> dict[str, Any]:
    return {
        "source": src.source_type.value,
        "label": src.label,
        "source_id": str(src.id),
        "skipped": True,
        "reason": reason,
    }


async def _run_collector(
    src: ProjectSource,
    collect_fn: Callable[[str], Any],
    *,
    is_async: bool = False,
) -> dict[str, Any]:
    creds = decrypt_normalized(src.credentials_encrypted)
    if src.source_type == SourceType.EMAIL:
        creds = prepare_email_creds(creds)
    if not can_collect(src.source_type, creds):
        data = _skipped_entry(
            src,
            f"Не заполнено для сбора: {missing_labels(src.source_type, creds)}",
        )
        log_collect_block(data, context=src.label or str(src.id))
        return data
    try:
        if is_async:
            data = await collect_fn(src.credentials_encrypted)
        else:
            data = collect_fn(src.credentials_encrypted)
        if data.get("source") != "youtrack":
            log_collect_block(data, context=src.label or str(src.id))
        return data
    except Exception as e:
        logger.exception("Collector %s failed for source %s", src.source_type.value, src.id)
        data = {
            **_skipped_entry(src, str(e)),
            "error": True,
        }
        log_collect_block(data, context=src.label or str(src.id))
        return data


async def collect_project_data(
    sources: list[ProjectSource],
    employees: list[Employee] | None = None,
    *,
    session: Session | None = None,
    project_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "youtrack": [],
        "email": [],
        "employee_emails": [],
        "calendar": [],
    }
    logger.info(
        "Collect project start | project_id=%s sources=%s employees=%s",
        project_id,
        len(sources),
        len(employees or []),
    )

    async_tasks: list[Awaitable[dict[str, Any]]] = []
    sync_jobs: list[tuple[str, ProjectSource]] = []

    for src in sources:
        if src.source_type == SourceType.YOUTRACK:
            async_tasks.append(_run_collector(src, collect_youtrack, is_async=True))
        elif src.source_type == SourceType.EMAIL:
            sync_jobs.append(("email", src))
        elif src.source_type == SourceType.CALENDAR:
            sync_jobs.append(("calendar", src))

    if async_tasks:
        results = await asyncio.gather(*async_tasks)
        for data in results:
            st = data.get("source")
            if st == "youtrack":
                snapshot["youtrack"].append(data)
                log_collect_block(data, context=f"project_id={project_id}")

    for kind, src in sync_jobs:
        data = await _run_collector(
            src,
            collect_email if kind == "email" else collect_calendar,
        )
        snapshot[kind].append(data)

    for emp in employees or []:
        try:
            mail_data = collect_employee_yandex_mail(emp)
            snapshot["employee_emails"].append(mail_data)
        except Exception as e:
            logger.exception("Employee mail %s failed", emp.id)
            snapshot["employee_emails"].append(
                {
                    "source": "employee_email",
                    "employee": emp.full_name,
                    "mailbox": emp.yandex_email,
                    "skipped": True,
                    "error": True,
                    "reason": str(e),
                    "messages": [],
                    "meetings": [],
                }
            )
        try:
            cal_data = collect_employee_yandex_calendar(emp)
            snapshot["calendar"].append(cal_data)
        except Exception as e:
            logger.exception("Employee calendar %s failed", emp.id)
            err_block = {
                "source": "employee_calendar",
                "employee": emp.full_name,
                "skipped": True,
                "error": True,
                "reason": str(e),
                "events": [],
            }
            log_collect_block(err_block, context=emp.full_name)
            snapshot["calendar"].append(err_block)

    _merge_email_meetings_into_calendar(snapshot)
    if session is not None and project_id is not None:
        enrich_snapshot_with_roadmap_table(snapshot, session, project_id)
        enrich_snapshot_with_milestones(snapshot, session, project_id)
    log_snapshot_summary(snapshot, project=str(project_id))
    return _truncate_snapshot(snapshot)


def _merge_email_meetings_into_calendar(snapshot: dict[str, Any]) -> None:
    """Встречи из календарных приглашений в письмах попадают в calendar для отчёта."""
    for block in snapshot.get("email", []):
        if block.get("skipped"):
            continue
        meetings = block.pop("meetings", None)
        if meetings:
            invite_block = {
                "source": "email_invites",
                "mailbox": block.get("mailbox"),
                "label": block.get("label"),
                "collected_at": block.get("collected_at"),
                "events": meetings,
                "events_count": len(meetings),
            }
            snapshot["calendar"].append(invite_block)
            logger.info(
                "Calendar merge email_invites | mailbox=%s events=%s",
                block.get("mailbox"),
                len(meetings),
            )

    for block in snapshot.get("employee_emails", []):
        if block.get("skipped"):
            continue
        meetings = block.pop("meetings", None)
        if meetings:
            invite_block = {
                "source": "employee_email_invites",
                "employee": block.get("employee"),
                "mailbox": block.get("mailbox"),
                "collected_at": block.get("collected_at"),
                "events": meetings,
                "events_count": len(meetings),
            }
            snapshot["calendar"].append(invite_block)
            logger.info(
                "Calendar merge employee_invites | %s events=%s",
                block.get("employee"),
                len(meetings),
            )
