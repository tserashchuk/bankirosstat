"""Сбор истории изменений YouTrack (activitiesPage) за период — отдельный шаг после search."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.services.collectors.ical_utils import COLLECTION_PERIOD_DAYS

logger = logging.getLogger(__name__)

# myfin: categories обязателен; activitiesPage вместо /activities (404 на myfin)
_ACTIVITY_CATEGORIES = (
    "IssueCreatedCategory,CustomFieldCategory,IssueResolvedCategory,CommentsCategory"
)
_ACTIVITY_PAGE_FIELDS = (
    "activities(timestamp,field(name),added(name),removed(name),author(name),target(text))"
)
# fallback для инстансов без activitiesPage
_ACTIVITY_FIELDS = (
    "id,timestamp,author(name,login),field(name),added(name),removed(name),target(text)"
)
_MAX_ISSUES_FOR_ACTIVITIES = 40
_MAX_COMMENT_TEXT = 500

_CLOSED_STATE_NAMES = frozenset(
    s.lower()
    for s in (
        "готово",
        "done",
        "closed",
        "resolved",
        "решена",
        "закрыта",
        "cancelled",
        "canceled",
        "отменена",
    )
)
_IN_PROGRESS_STATE_NAMES = frozenset(
    s.lower()
    for s in (
        "в работе",
        "in progress",
        "progress",
        "doing",
        "разработка",
    )
)
_STATUS_FIELD_NAMES = frozenset(s.lower() for s in ("state", "статус", "status"))


def _activity_error_detail(resp: httpx.Response) -> str:
    try:
        err = resp.json()
        parts = [
            err.get("error_description"),
            err.get("error_developer_message"),
            err.get("error"),
        ]
        return " — ".join(p for p in parts if p) or resp.text[:200]
    except Exception:
        return resp.text[:200]


def _activity_ts_ms(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            ms = int(raw)
            return ms if ms > 10_000_000_000 else ms * 1000
        if isinstance(raw, str) and raw.isdigit():
            ms = int(raw)
            return ms if ms > 10_000_000_000 else ms * 1000
    except (TypeError, ValueError):
        return None
    return None


def _activity_in_period(ts_ms: int | None, cutoff: datetime) -> bool:
    if ts_ms is None:
        return False
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt >= cutoff


def _state_name(blob: Any) -> str:
    if isinstance(blob, dict):
        return (blob.get("name") or "").strip()
    if blob is None:
        return ""
    return str(blob).strip()


def _bundle_names(blob: Any) -> str:
    """myfin: added/removed часто массив [{name, $type}, ...]."""
    if isinstance(blob, list):
        names = [_state_name(x) for x in blob if x]
        return ", ".join(n for n in names if n)
    return _state_name(blob)


def _author_name(author: Any) -> str:
    if isinstance(author, dict):
        return (author.get("name") or author.get("login") or "").strip()
    return ""


def _comment_text(item: dict[str, Any]) -> str:
    target = item.get("target")
    if isinstance(target, dict):
        text = (target.get("text") or "").strip()
        if text:
            return text[:_MAX_COMMENT_TEXT]
    added = item.get("added")
    if isinstance(added, str) and added.strip():
        return added.strip()[:_MAX_COMMENT_TEXT]
    return ""


def _field_info(field: Any) -> tuple[str, str]:
    if not isinstance(field, dict):
        return "", ""
    return (field.get("name") or "").strip(), str(field.get("$type") or "")


def _is_status_field(field_name: str) -> bool:
    return field_name.lower() in _STATUS_FIELD_NAMES


def _parse_activity_item(item: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    ts_ms = _activity_ts_ms(item.get("timestamp"))
    if not _activity_in_period(ts_ms, cutoff):
        return None

    at = (
        datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
        if ts_ms
        else None
    )
    field_name, field_type = _field_info(item.get("field"))
    field_name = (item.get("targetMember") or "").strip() or field_name

    category = ""
    cat = item.get("category")
    if isinstance(cat, dict):
        category = str(cat.get("id") or "")
    elif isinstance(cat, str):
        category = cat.strip()

    type_name = str(item.get("$type") or "")
    type_l = type_name.lower()
    is_comment = "comment" in type_l or "commentscategory" in category.lower()
    is_synthetic = field_type == "PredefinedFilterField"

    added = _bundle_names(item.get("added"))
    removed = _bundle_names(item.get("removed"))
    author = _author_name(item.get("author"))
    text = _comment_text(item) if is_comment else ""

    return {
        "at": at,
        "field": field_name,
        "field_type": field_type,
        "added": added,
        "removed": removed,
        "category": category,
        "type": type_name,
        "author": author,
        "text": text,
        "is_comment": is_comment,
        "is_synthetic": is_synthetic,
        "is_status_field": _is_status_field(field_name),
    }


def _summarize_period_events(
    activities: list[dict[str, Any]], *, current_state: str | None
) -> dict[str, Any]:
    """Классификация за период для блока «созданы / в работу / закрыты» + комментарии."""
    created_in_period = False
    closed_in_period = False
    started_in_period = False
    state_changes: list[dict[str, str]] = []
    comments_in_period: list[dict[str, str]] = []

    for act in activities:
        type_name = (act.get("type") or "").lower()
        category = (act.get("category") or "").lower()

        if act.get("is_comment"):
            text = (act.get("text") or "").strip()
            if text or act.get("author"):
                comments_in_period.append(
                    {
                        "at": act.get("at") or "",
                        "author": act.get("author") or "",
                        "text": text[:_MAX_COMMENT_TEXT],
                    }
                )
            continue

        if act.get("is_synthetic"):
            if "issuecreated" in type_name:
                created_in_period = True
            if "issueresolved" in type_name:
                closed_in_period = True
            continue

        if "issuecreated" in type_name or "issuecreated" in category:
            created_in_period = True

        if "issueresolved" in type_name or "resolved" in type_name:
            closed_in_period = True

        if not act.get("is_status_field"):
            continue

        added = (act.get("added") or "").strip()
        removed = (act.get("removed") or "").strip()
        if added or removed:
            state_changes.append(
                {
                    "from": removed,
                    "to": added,
                    "at": act.get("at") or "",
                }
            )
        for part in added.split(","):
            part = part.strip().lower()
            if part in _CLOSED_STATE_NAMES:
                closed_in_period = True
            if part in _IN_PROGRESS_STATE_NAMES:
                started_in_period = True

        added_l = added.lower()
        removed_l = removed.lower()
        if added_l in _CLOSED_STATE_NAMES and removed_l not in _CLOSED_STATE_NAMES:
            closed_in_period = True
        if added_l in _IN_PROGRESS_STATE_NAMES and removed_l not in _IN_PROGRESS_STATE_NAMES:
            started_in_period = True

    return {
        "created_in_period": created_in_period,
        "started_in_period": started_in_period,
        "closed_in_period": closed_in_period,
        "state_changes": state_changes[:20],
        "comments_in_period": comments_in_period[:15],
        "current_state": current_state,
    }


def _parse_activities_response(
    data: Any, cutoff: datetime
) -> list[dict[str, Any]]:
    raw_items: list[Any]
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("activities") or data.get("data") or []
    else:
        return []

    out: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        parsed = _parse_activity_item(item, cutoff)
        if parsed:
            out.append(parsed)
    return out


async def _fetch_activities_from_url(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, str | int],
    *,
    cutoff: datetime,
    log_errors: bool,
    issue_ref: str,
) -> tuple[list[dict[str, Any]], int | None]:
    resp = await client.get(url, headers=headers, params=params)
    if resp.status_code == 404:
        return [], 404
    if resp.status_code != 200:
        if log_errors:
            logger.warning(
                "YouTrack activities %s HTTP %s: %s",
                issue_ref,
                resp.status_code,
                _activity_error_detail(resp),
            )
        return [], resp.status_code

    try:
        data = resp.json()
    except Exception:
        return [], 200
    return _parse_activities_response(data, cutoff), 200


async def fetch_issue_activities(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    issue_ref: str,
    *,
    cutoff: datetime,
    end: datetime | None = None,
    log_errors: bool = True,
) -> list[dict[str, Any]]:
    """activitiesPage (myfin), fallback — /activities."""
    ref = (issue_ref or "").strip()
    if not ref:
        return []

    end_dt = end or datetime.now(timezone.utc)
    common: dict[str, str | int] = {
        "categories": _ACTIVITY_CATEGORIES,
        "$top": 50,
        "start": str(int(cutoff.timestamp() * 1000)),
        "end": str(int(end_dt.timestamp() * 1000)),
    }

    page_params = {**common, "fields": _ACTIVITY_PAGE_FIELDS}
    items, status = await _fetch_activities_from_url(
        client,
        f"{base_url}/api/issues/{ref}/activitiesPage",
        headers,
        page_params,
        cutoff=cutoff,
        log_errors=log_errors,
        issue_ref=ref,
    )
    if items:
        return items
    if status == 404:
        legacy_params = {**common, "fields": _ACTIVITY_FIELDS}
        items, _ = await _fetch_activities_from_url(
            client,
            f"{base_url}/api/issues/{ref}/activities",
            headers,
            legacy_params,
            cutoff=cutoff,
            log_errors=log_errors,
            issue_ref=ref,
        )
        return items
    return []


async def enrich_issues_with_period_activities(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    issues: list[dict[str, Any]],
    *,
    days: int = COLLECTION_PERIOD_DAYS,
    max_issues: int = _MAX_ISSUES_FOR_ACTIVITIES,
) -> list[dict[str, Any]]:
    """
    Отдельный проход: для каждой задачи — activitiesPage за последние N дней.
    Добавляет поле period_events в issue dict.
    """
    if not issues:
        return issues

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    end = datetime.now(timezone.utc)
    to_process = issues[:max_issues]
    skipped = max(0, len(issues) - len(to_process))

    enriched: list[dict[str, Any]] = []
    ok_count = 0
    comment_count = 0

    for issue in to_process:
        row = dict(issue)
        issue_ref = str(row.get("id") or "")
        activities = await fetch_issue_activities(
            client,
            base_url,
            headers,
            issue_ref,
            cutoff=cutoff,
            end=end,
            log_errors=(issue is to_process[0]),
        )
        period = _summarize_period_events(
            activities, current_state=row.get("state")
        )
        row["period_events"] = period
        if activities:
            ok_count += 1
        if period.get("comments_in_period"):
            comment_count += 1
        enriched.append(row)

    enriched.extend(issues[len(to_process) :])

    logger.info(
        "YouTrack activitiesPage: обогащено %s/%s (пропущено %s, с событиями %s, с комментариями %s)",
        len(to_process),
        len(issues),
        skipped,
        ok_count,
        comment_count,
    )
    return enriched
