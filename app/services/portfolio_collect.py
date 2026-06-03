from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from sqlmodel import Session

from app.models import Employee, InternalProject, ProjectSource, SourceType
from app.services.aggregator import _run_collector, _truncate_snapshot
from app.services.collectors.calendar import collect_calendar
from app.services.collectors.email_imap import collect_email
from app.services.collectors.employee_yandex import (
    collect_employee_yandex_calendar,
    collect_employee_yandex_mail,
)
from app.services.collectors.youtrack import collect_youtrack
from app.services.roadmap_normalize import enrich_snapshot_with_roadmap_table
from app.services.collectors.collect_log import log_collect_block, log_snapshot_summary
from app.services.collectors.ical_utils import (
    event_involves_participant,
    participant_email_variants,
)
from app.services.project_classifier import (
    ProjectProfile,
    SharedPool,
    build_project_profiles,
    merge_email_meetings,
    route_pool_to_snapshots,
)

logger = logging.getLogger(__name__)


def _empty_bound() -> dict[str, Any]:
    return {
        "youtrack": [],
        "email": [],
        "calendar": [],
    }


async def _collect_bound_sources(sources: list[ProjectSource]) -> dict[str, Any]:
    """Источники, жёстко привязанные к проекту: YouTrack (roadmap — из БД)."""
    bound = _empty_bound()
    tasks: list[Any] = []

    for src in sources:
        if src.source_type == SourceType.YOUTRACK:
            tasks.append(("youtrack", src, collect_youtrack, True))

    async def _one(kind: str, src: ProjectSource, fn, is_async: bool):
        return kind, await _run_collector(src, fn, is_async=is_async)

    if tasks:
        results = await asyncio.gather(*[_one(*t) for t in tasks])
        for kind, data in results:
            if data.get("skipped"):
                continue
            if kind == "youtrack":
                bound["youtrack"].append(data)

    return bound


async def collect_portfolio_snapshots(
    targets: list[tuple[InternalProject, list[ProjectSource], list[Employee]]],
    *,
    session: Session,
) -> dict[uuid.UUID, dict[str, Any]]:
    """
    Сбор по портфелю: без дублирования почты сотрудников и IMAP,
    затем автораспределение писем и встреч по проектам.
    """
    profiles = build_project_profiles(targets)
    pool = SharedPool()
    bound_by_project: dict[uuid.UUID, dict[str, Any]] = {}

    seen_employees: set[uuid.UUID] = set()
    seen_mail_creds: set[str] = set()
    employee_project_ids: dict[uuid.UUID, list[uuid.UUID]] = {}
    for project, _, employees in targets:
        for emp in employees:
            employee_project_ids.setdefault(emp.id, []).append(project.id)

    logger.info(
        "Portfolio collect start | projects=%s",
        len(targets),
    )

    for project, sources, employees in targets:
        pid = project.id
        logger.info(
            "Portfolio project | %r sources=%s employees=%s",
            project.name,
            len(sources),
            len(employees),
        )
        bound_by_project[pid] = await _collect_bound_sources(sources)
        for yt in bound_by_project[pid].get("youtrack") or []:
            log_collect_block(yt, context=project.name)

        for src in sources:
            if src.source_type != SourceType.EMAIL:
                if src.source_type == SourceType.CALENDAR:
                    cred_key = f"cal:{src.credentials_encrypted[:48]}"
                    if cred_key in seen_mail_creds:
                        logger.info(
                            "Collect calendar: SKIPPED duplicate cred | project=%r source=%s",
                            project.name,
                            src.id,
                        )
                        continue
                    seen_mail_creds.add(cred_key)
                    try:
                        data = await _run_collector(
                            src, collect_calendar, is_async=False
                        )
                        if data.get("skipped"):
                            log_collect_block(
                                data, context=f"{project.name} source={src.id}"
                            )
                        else:
                            n = 0
                            for ev in data.get("events", []):
                                pool.calendar_events.append(
                                    {
                                        **ev,
                                        "_hint_project_id": str(pid),
                                        "_origin": "project_calendar",
                                    }
                                )
                                n += 1
                            logger.info(
                                "Portfolio calendar pool +%s events | project=%r",
                                n,
                                project.name,
                            )
                    except Exception as e:
                        logger.exception("Calendar source %s failed", src.id)
                continue

            cred_key = f"mail:{src.credentials_encrypted[:64]}"
            if cred_key in seen_mail_creds:
                logger.info(
                    "Collect email: SKIPPED duplicate cred | project=%r source=%s",
                    project.name,
                    src.id,
                )
                continue
            seen_mail_creds.add(cred_key)

            try:
                data = await _run_collector(src, collect_email, is_async=False)
                if data.get("skipped"):
                    log_collect_block(data, context=f"{project.name} source={src.id}")
                    continue
                bound_by_project[pid]["email"].append(data)
                meetings = data.pop("meetings", None) or []
                if meetings:
                    bound_by_project[pid]["calendar"].append(
                        {
                            "source": "email_invites",
                            "mailbox": data.get("mailbox"),
                            "events": meetings,
                            "events_count": len(meetings),
                        }
                    )
                    logger.info(
                        "Portfolio email_invites +%s | project=%r mailbox=%s",
                        len(meetings),
                        project.name,
                        data.get("mailbox"),
                    )
            except Exception as e:
                logger.exception("Email source %s failed", src.id)

        for emp in employees:
            if emp.id in seen_employees:
                logger.info(
                    "Collect employee: SKIPPED duplicate | %s",
                    emp.yandex_email,
                )
                continue
            seen_employees.add(emp.id)

            try:
                mail_data = collect_employee_yandex_mail(emp)
                messages = mail_data.pop("messages", []) or []
                meetings = mail_data.pop("meetings", []) or []

                for msg in messages:
                    pool.messages.append(
                        {
                            **msg,
                            "_employee_id": str(emp.id),
                            "_employee_email": emp.yandex_email,
                            "_employee_name": emp.full_name,
                            "_origin": "employee_mail",
                        }
                    )
                variants = participant_email_variants(emp.yandex_email or "")
                for m in meetings:
                    if variants and not event_involves_participant(
                        m, variants, trust_own_calendar=False
                    ):
                        continue
                    pool.calendar_events.append(
                        {
                            **m,
                            "_employee_id": str(emp.id),
                            "_employee_email": emp.yandex_email,
                            "_origin": "employee_invite",
                        }
                    )
                if meetings:
                    logger.info(
                        "Portfolio employee_invites +%s | %s",
                        len(meetings),
                        emp.yandex_email,
                    )

            except Exception as e:
                logger.exception("Employee mail %s failed", emp.id)

            try:
                cal_data = collect_employee_yandex_calendar(emp)
                if cal_data.get("skipped"):
                    pass
                else:
                    n = 0
                    hint_ids = employee_project_ids.get(emp.id) or []
                    hint = str(hint_ids[0]) if len(hint_ids) == 1 else None
                    for ev in cal_data.get("events", []):
                        row = {
                            **ev,
                            "_employee_id": str(emp.id),
                            "_employee_email": emp.yandex_email,
                            "_origin": "employee_calendar",
                        }
                        if hint:
                            row["_hint_project_id"] = hint
                        pool.calendar_events.append(row)
                        n += 1
                    logger.info(
                        "Portfolio employee_calendar pool +%s | %s",
                        n,
                        emp.yandex_email,
                    )
            except Exception as e:
                logger.exception("Employee calendar %s failed", emp.id)

    logger.info(
        "Portfolio pool totals | messages=%s calendar_events=%s",
        len(pool.messages),
        len(pool.calendar_events),
    )

    snapshots = route_pool_to_snapshots(profiles, pool, bound_by_project)

    for project, _, _ in targets:
        pid = project.id
        if pid not in snapshots:
            continue
        snap = snapshots[pid]
        merge_email_meetings(snap)
        enrich_snapshot_with_roadmap_table(snap, session, pid)
        snapshots[pid] = _truncate_snapshot(snap)
        log_snapshot_summary(snap, project=project.name)

    logger.info("Portfolio collect done | snapshots=%s", len(snapshots))
    return snapshots
