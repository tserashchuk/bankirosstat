"""Единое INFO-логирование результатов сбора данных."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _count_events(data: dict[str, Any]) -> int:
    if "events_count" in data:
        return int(data.get("events_count") or 0)
    events = data.get("events")
    return len(events) if isinstance(events, list) else 0


def log_collect_block(data: dict[str, Any], *, context: str = "") -> None:
    """Логирует итог одного блока сбора (источник, сотрудник, проект)."""
    source = data.get("source") or "unknown"
    ctx = f" | {context}" if context else ""

    if data.get("error"):
        logger.warning(
            "Collect %s%s: ERROR — %s",
            source,
            ctx,
            data.get("reason") or data.get("error"),
        )
        return

    if data.get("skipped"):
        logger.info(
            "Collect %s%s: SKIPPED — %s",
            source,
            ctx,
            data.get("reason") or "пропущено",
        )
        return

    if source in ("calendar", "employee_calendar"):
        n = _count_events(data)
        login = data.get("username") or data.get("mailbox") or data.get("employee") or "?"
        mode = data.get("protocol") or data.get("provider") or "ical"
        days = data.get("period_days", "?")
        logger.info(
            "Collect %s%s: OK — %s событий за %s дн. (%s, %s)",
            source,
            ctx,
            n,
            days,
            login,
            mode,
        )
        return

    if source in ("email", "employee_email"):
        n_msg = data.get("messages_count", len(data.get("messages") or []))
        n_meet = data.get("meetings_count", len(data.get("meetings") or []))
        login = data.get("mailbox") or data.get("employee") or "?"
        logger.info(
            "Collect %s%s: OK — писем %s, встреч из invite %s (%s)",
            source,
            ctx,
            n_msg,
            n_meet,
            login,
        )
        return

    if source == "youtrack":
        issues = data.get("issues") or []
        n = len(issues) if isinstance(issues, list) else 0
        mode = data.get("collection_mode") or "?"
        component = data.get("component") or ""
        extra = f", component={component!r}" if component else ""
        logger.info(
            "Collect youtrack%s: OK — %s задач (%s%s)",
            ctx,
            n,
            mode,
            extra,
        )
        return

    logger.info("Collect %s%s: OK", source, ctx)


def log_snapshot_summary(snapshot: dict[str, Any], *, project: str) -> None:
    """Сводка по snapshot перед отправкой в LLM."""
    yt_issues = 0
    for block in snapshot.get("youtrack") or []:
        if isinstance(block, dict) and not block.get("skipped"):
            yt_issues += len(block.get("issues") or [])

    cal_events = 0
    for block in snapshot.get("calendar") or []:
        if isinstance(block, dict) and not block.get("skipped"):
            cal_events += _count_events(block)

    email_msgs = 0
    for block in snapshot.get("email") or []:
        if isinstance(block, dict) and not block.get("skipped"):
            email_msgs += len(block.get("messages") or [])
    for block in snapshot.get("employee_emails") or []:
        if isinstance(block, dict) and not block.get("skipped"):
            email_msgs += len(block.get("messages") or [])

    roadmap = len(snapshot.get("roadmap_table") or [])
    milestones = len(snapshot.get("milestones") or [])
    logger.info(
        "Snapshot %r: youtrack=%s issues, calendar=%s events, email=%s messages, roadmap=%s rows, milestones=%s",
        project,
        yt_issues,
        cal_events,
        email_msgs,
        roadmap,
        milestones,
    )
