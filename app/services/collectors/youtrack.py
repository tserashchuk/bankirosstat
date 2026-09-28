from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.services.collectors.ical_utils import COLLECTION_PERIOD_DAYS
from app.services.collectors.youtrack_activities import (
    enrich_issues_with_period_activities,
    is_in_progress_state,
    issue_has_period_activity,
    issue_is_in_progress,
)
from app.services.source_creds import decrypt_normalized

logger = logging.getLogger(__name__)

# myfin: в Postman работает updated: {This week} (week с маленькой w)
_YOUTRACK_UPDATED_FILTERS = (
    "updated: {This week}",
    "Updated: {This Week}",
    "обновлена: {На этой неделе}",
)
_IN_PROGRESS_SEARCH_CLAUSES = (
    "State: {In Progress}",
    "Состояние: {В работе}",
    "state: {В работе}",
)
_INTERNAL_PROJECT_ID_RE = re.compile(r"^\d+-\d+$")
_ISSUE_FIELDS = (
    "id,idReadable,summary,updated,"
    "customFields(projectCustomField(field(name)),value(name,summary,text)),"
    "comments(text,created)"
)


def _normalize_youtrack_base_url(url: str) -> str:
    base = (url or "").strip().rstrip("/")
    for marker in ("/agiles/", "/issue/", "/issues", "/youtrack/"):
        idx = base.lower().find(marker.lower())
        if idx > 0:
            base = base[:idx].rstrip("/")
            break
    return base


def _format_component_clauses(component: str) -> tuple[str, str]:
    """Компонент:{Имя} и Компонент: {Имя} — оба варианта для probe."""
    raw = (component or "").strip().strip('"')
    if not raw:
        return "", ""
    inner = raw.strip("{}")
    brace = f"{{{inner}}}"
    return f"Компонент:{brace}", f"Компонент: {brace}"


def _extract_search_query_from_url(url: str) -> str:
    """query= из URL (например с доски agile)."""
    parsed = urlparse((url or "").strip())
    values = parse_qs(parsed.query).get("query")
    if not values:
        return ""
    return unquote(values[0]).strip()


def _search_query_has_date_filter(query: str) -> bool:
    q = (query or "").lower()
    return "обновлена:" in q or "updated:" in q


def _build_component_search_queries(
    component_name: str, *, url_query: str = ""
) -> list[str]:
    """Поиск по полю Компонент + обновление за неделю (как в UI/Postman)."""
    tight, spaced = _format_component_clauses(component_name)
    if not tight:
        return []

    queries: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            queries.append(q)

    extra = (url_query or "").strip()
    comp_variants = (tight, spaced)

    if extra:
        for comp in comp_variants:
            add(f"{extra} {comp}")
            add(f"{comp} {extra}")

    for week in _YOUTRACK_UPDATED_FILTERS:
        for comp in comp_variants:
            add(f"{week} {comp}")
            add(f"{comp} {week}")

    for state_clause in _IN_PROGRESS_SEARCH_CLAUSES:
        for comp in comp_variants:
            add(f"{comp} {state_clause}")
            add(f"{state_clause} {comp}")

    for comp in comp_variants:
        add(comp)

    return queries


def _format_project_clause(project_ref: str) -> str:
    pid = (project_ref or "").strip()
    if not pid:
        return ""
    if pid.startswith("{") and pid.endswith("}"):
        return f"project: {pid}"
    if " " in pid:
        return f"project: {{{pid}}}"
    return f"project: {pid}"


def _build_project_search_queries(project_ref: str) -> list[str]:
    clause = _format_project_clause(project_ref)
    if not clause:
        return []
    queries: list[str] = []
    seen: set[str] = set()
    for week in _YOUTRACK_UPDATED_FILTERS:
        q = f"{clause} {week}"
        if q not in seen:
            seen.add(q)
            queries.append(q)
    return queries


def _parse_issues_response(resp: httpx.Response) -> list[dict[str, Any]]:
    content_type = (resp.headers.get("content-type") or "").lower()
    text = (resp.text or "").strip()
    if not text:
        raise ValueError("YouTrack вернул пустой ответ (проверьте URL инстанса)")
    if "json" not in content_type and text.startswith("<"):
        raise ValueError(
            "YouTrack вернул HTML вместо JSON — укажите корень инстанса "
            f"(https://host). Начало: {text[:120]!r}"
        )
    try:
        data = resp.json()
    except json.JSONDecodeError as e:
        raise ValueError(
            f"YouTrack: ответ не JSON ({e}). Content-Type: {content_type!r}, "
            f"начало: {text[:200]!r}"
        ) from e
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("issues", "data", "result", "value"):
            val = data.get(key)
            if isinstance(val, list):
                return val
    return []


def _youtrack_error_detail(resp: httpx.Response) -> str:
    try:
        err = resp.json()
        parts = [err.get("error_description"), err.get("error_developer_message"), err.get("error")]
        return " — ".join(p for p in parts if p) or resp.text[:300]
    except json.JSONDecodeError:
        return resp.text[:300]


def _issue_updated_dt(issue: dict[str, Any]) -> datetime | None:
    raw = issue.get("updated")
    if raw is None:
        return None
    try:
        if isinstance(raw, (int, float)):
            ms = int(raw)
            if ms > 10_000_000_000:
                ms //= 1000
            return datetime.fromtimestamp(ms, tz=timezone.utc)
        if isinstance(raw, str):
            if raw.isdigit():
                ms = int(raw)
                if ms > 10_000_000_000:
                    ms //= 1000
                return datetime.fromtimestamp(ms, tz=timezone.utc)
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OSError):
        return None
    return None


def _filter_issues_by_period(
    issues: list[dict[str, Any]], days: int = COLLECTION_PERIOD_DAYS
) -> list[dict[str, Any]]:
    """Фильтр по updated (до обогащения activities)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict[str, Any]] = []
    for issue in issues:
        updated = _issue_updated_dt(issue)
        if updated is None or updated >= cutoff:
            out.append(issue)
    dropped = len(issues) - len(out)
    if dropped:
        logger.info(
            "YouTrack: отфильтровано %s задач старше %s дн. по updated (осталось %s)",
            dropped,
            days,
            len(out),
        )
    return out


def _filter_issues_after_enrichment(
    issues: list[dict[str, Any]], days: int = COLLECTION_PERIOD_DAYS
) -> list[dict[str, Any]]:
    """Оставляет задачи с активностью за период, в работе сейчас или недавним updated."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[dict[str, Any]] = []
    in_progress_kept = 0
    for issue in issues:
        if issue_has_period_activity(issue.get("period_events")):
            out.append(issue)
            continue
        if issue_is_in_progress(issue):
            out.append(issue)
            in_progress_kept += 1
            continue
        updated = _issue_updated_dt(issue)
        if updated is None or updated >= cutoff:
            out.append(issue)
    dropped = len(issues) - len(out)
    if dropped or in_progress_kept:
        logger.info(
            "YouTrack: после activities отброшено %s, оставлено в работе %s (итого %s)",
            dropped,
            in_progress_kept,
            len(out),
        )
    return out


async def _resolve_project_ref(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    project_id: str,
) -> str:
    raw = (project_id or "").strip()
    if not raw:
        return ""
    inner = raw.strip("{}").strip()
    if not _INTERNAL_PROJECT_ID_RE.match(inner):
        return inner

    resp = await client.get(
        f"{base_url}/api/admin/projects/{inner}",
        headers=headers,
        params={"fields": "name,shortName"},
    )
    if resp.status_code != 200:
        logger.warning(
            "YouTrack project lookup %s failed HTTP %s: %s",
            inner,
            resp.status_code,
            _youtrack_error_detail(resp),
        )
        return inner

    data = resp.json()
    name = (data.get("name") or "").strip()
    if name:
        logger.info("YouTrack project %s resolved to name %r", inner, name)
        return name
    short = (data.get("shortName") or "").strip()
    if short:
        return short
    return inner


def _custom_field_name(field_entry: dict[str, Any]) -> str:
    name = (field_entry.get("name") or "").strip()
    if name:
        return name
    pcf = field_entry.get("projectCustomField")
    if isinstance(pcf, dict):
        inner = pcf.get("field")
        if isinstance(inner, dict):
            return (inner.get("name") or "").strip()
    return ""


def _extract_component_names(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [
            str(v.get("name"))
            for v in value
            if isinstance(v, dict) and v.get("name")
        ]
    if isinstance(value, dict) and value.get("name"):
        return [str(value["name"])]
    return []


def _clean_issue(issue: dict[str, Any]) -> dict[str, Any]:
    fields = issue.get("customFields") or issue.get("fields") or []
    state = assignee = spent = epic = None
    components: list[str] = []

    for f in fields:
        if not isinstance(f, dict):
            continue
        name = _custom_field_name(f).lower()
        value = f.get("value")
        if name == "state" and value:
            state = value.get("name") if isinstance(value, dict) else str(value)
        elif name == "assignee" and value:
            assignee = value.get("name") if isinstance(value, dict) else str(value)
        elif name in ("компонент", "component") and value:
            components = _extract_component_names(value)
        elif "spent" in name and value:
            spent = value
        elif name == "epic" and value:
            epic = value.get("summary") or value.get("name") if isinstance(value, dict) else str(value)

    if not state and issue.get("state"):
        s = issue["state"]
        state = s.get("name") if isinstance(s, dict) else str(s)

    comments = issue.get("comments") or []
    last_comment = None
    if comments:
        c = comments[-1]
        last_comment = (c.get("text") or "")[:500]

    return {
        "id": issue.get("idReadable") or issue.get("numberInProject") or issue.get("id"),
        "summary": issue.get("summary"),
        "state": state,
        "in_progress": is_in_progress_state(state),
        "assignee": assignee,
        "components": components,
        "spent_time": spent,
        "epic": epic,
        "updated": issue.get("updated"),
        "last_comment": last_comment,
    }


async def _fetch_search_issues(
    client: httpx.AsyncClient,
    api_url: str,
    headers: dict[str, str],
    query: str,
    *,
    skip: int,
    page_size: int,
) -> httpx.Response:
    return await client.get(
        api_url,
        headers=headers,
        params={
            "query": query,
            "fields": _ISSUE_FIELDS,
            "$skip": skip,
            "$top": page_size,
        },
    )


async def _collect_via_search_queries(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    queries: list[str],
    *,
    filter_period: bool,
    probe_until_nonempty: bool = False,
) -> tuple[list[dict[str, Any]], str]:
    api_url = f"{base_url}/api/issues"
    page_size = 50
    active_query: str | None = None
    first_resp: httpx.Response | None = None
    best_undated: tuple[str, httpx.Response, int] | None = None
    fallback_dated: tuple[str, httpx.Response] | None = None
    last_error = ""

    for candidate in queries:
        resp = await _fetch_search_issues(
            client, api_url, headers, candidate, skip=0, page_size=page_size
        )
        logger.info("YouTrack search probe HTTP %s | query=%r", resp.status_code, candidate)
        if resp.status_code != 200:
            last_error = _youtrack_error_detail(resp)
            logger.warning("YouTrack search rejected: %s", last_error)
            continue
        batch = _parse_issues_response(resp)
        has_date = _search_query_has_date_filter(candidate)
        if batch:
            logger.info("YouTrack search probe: %s issues | query=%r", len(batch), candidate)
            if has_date:
                active_query = candidate
                first_resp = resp
                break
            if best_undated is None or len(batch) > best_undated[2]:
                best_undated = (candidate, resp, len(batch))
            if not probe_until_nonempty:
                active_query = candidate
                first_resp = resp
                break
            continue
        logger.info("YouTrack search probe empty | query=%r", candidate)
        if has_date and fallback_dated is None:
            fallback_dated = (candidate, resp)
        if not probe_until_nonempty:
            active_query = candidate
            first_resp = resp
            break

    if not active_query and best_undated:
        active_query, first_resp, n = best_undated
        logger.info(
            "YouTrack search: недельные query пустые, берём без даты (%s задач) | query=%r",
            n,
            active_query,
        )
    elif not active_query and fallback_dated:
        active_query, first_resp = fallback_dated
        logger.warning(
            "YouTrack search: все probe пустые, используем query=%r", active_query
        )

    if not active_query or not first_resp:
        raise RuntimeError(
            f"YouTrack: ни один search-query не принят ({last_error}). Пробовали: {queries}"
        )

    raw_issues: list[dict[str, Any]] = []
    skip = 0
    while True:
        resp = first_resp if skip == 0 else await _fetch_search_issues(
            client, api_url, headers, active_query, skip=skip, page_size=page_size
        )
        if resp.status_code == 400:
            raise RuntimeError(
                f"YouTrack invalid_query при пагинации: {_youtrack_error_detail(resp)}"
            )
        resp.raise_for_status()
        batch = _parse_issues_response(resp)
        if not batch:
            break
        raw_issues.extend(batch)
        logger.info("YouTrack search page: +%s (total %s)", len(batch), len(raw_issues))
        if len(batch) < page_size:
            break
        skip += page_size

    raw_count = len(raw_issues)
    # Предфильтр по updated только если в query уже нет даты и не ждём activitiesPage.
    apply_period = filter_period and not _search_query_has_date_filter(active_query)
    if apply_period:
        raw_issues = _filter_issues_by_period(raw_issues)
    logger.info(
        "YouTrack search: raw=%s after_updated_prefilter=%s prefilter=%s query=%r",
        raw_count,
        len(raw_issues),
        apply_period,
        active_query,
    )

    return [_clean_issue(i) for i in raw_issues], active_query


async def collect_youtrack(credentials_encrypted: str) -> dict[str, Any]:
    creds = decrypt_normalized(credentials_encrypted)
    source_url = creds.get("url", "")
    base_url = _normalize_youtrack_base_url(source_url)
    token = creds["token"]
    project_id = creds.get("project_id", "")
    component_name = (creds.get("component_name") or "").strip()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    collection_mode = ""
    query = ""
    project_ref = ""
    issues: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=120.0) as client:
        if component_name:
            url_query = _extract_search_query_from_url(source_url)
            search_queries = _build_component_search_queries(
                component_name, url_query=url_query
            )
            logger.info(
                "YouTrack component search | component=%r url_query=%r queries=%s",
                component_name,
                url_query or None,
                search_queries,
            )
            issues, query = await _collect_via_search_queries(
                client,
                base_url,
                headers,
                search_queries,
                filter_period=False,
                probe_until_nonempty=True,
            )
            collection_mode = "component_search"
        elif project_id:
            project_ref = await _resolve_project_ref(client, base_url, headers, project_id)
            search_queries = _build_project_search_queries(project_ref)
            if not search_queries:
                raise RuntimeError("YouTrack: укажите Компонент или Project ID")
            issues, query = await _collect_via_search_queries(
                client,
                base_url,
                headers,
                search_queries,
                filter_period=False,
                probe_until_nonempty=True,
            )
            collection_mode = "project_search"
        else:
            raise RuntimeError(
                "YouTrack: укажите Компонент (например Криптокарта) или Project ID"
            )

        if issues:
            issues = await enrich_issues_with_period_activities(
                client, base_url, headers, issues
            )
            issues = _filter_issues_after_enrichment(issues)

    if issues:
        preview = ", ".join(
            f"{i.get('id')}: {i.get('summary', '')[:60]}"
            for i in issues[:8]
        )
        if len(issues) > 8:
            preview += f", … (+{len(issues) - 8})"
        logger.info(
            "YouTrack collected %s issues (%s) | %s",
            len(issues),
            collection_mode,
            preview,
        )
    else:
        logger.info("YouTrack collected 0 issues mode=%s query=%r", collection_mode, query)

    return {
        "source": "youtrack",
        "instance_url": base_url,
        "collection_mode": collection_mode,
        "component": component_name,
        "project_id": project_id,
        "project_ref": project_ref,
        "query": query,
        "collected_at": datetime.utcnow().isoformat(),
        "period_days": COLLECTION_PERIOD_DAYS,
        "activities_enriched": sum(
            1 for i in issues if isinstance(i.get("period_events"), dict)
        ),
        "issues": issues,
    }


async def test_youtrack_connection(credentials_encrypted: str) -> tuple[bool, str]:
    from app.services.source_creds import missing_labels
    from app.models import SourceType

    creds = decrypt_normalized(credentials_encrypted)
    missing = missing_labels(SourceType.YOUTRACK, creds)
    if missing:
        return False, f"YouTrack: для проверки укажите {missing}"

    source_url = creds.get("url", "")
    base_url = _normalize_youtrack_base_url(source_url)
    token = creds["token"]
    component_name = (creds.get("component_name") or "").strip()

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

            if component_name:
                url_query = _extract_search_query_from_url(source_url)
                queries = _build_component_search_queries(
                    component_name, url_query=url_query
                )
                test_url = f"{base_url}/api/issues"
                resp: httpx.Response | None = None
                for q in queries:
                    logger.info("YouTrack test search GET query=%r", q)
                    resp = await client.get(
                        test_url,
                        headers=headers,
                        params={"query": q, "$top": 1},
                    )
                    if resp.status_code == 200 and _parse_issues_response(resp):
                        return True, f"YouTrack: OK (Компонент: {q})"
                if resp and resp.status_code == 200:
                    return True, f"YouTrack: OK (Компонент, пустой ответ: {queries[0]})"
                detail = _youtrack_error_detail(resp) if resp else "нет ответа"
                return False, f"YouTrack: поиск по компоненту — {detail}"

            if creds.get("project_id"):
                project_ref = await _resolve_project_ref(
                    client, base_url, headers, creds["project_id"]
                )
                queries = _build_project_search_queries(project_ref)
                test_url = f"{base_url}/api/issues"
                for q in queries:
                    resp = await client.get(
                        test_url,
                        headers=headers,
                        params={"query": q, "$top": 1},
                    )
                    if resp.status_code == 200:
                        return True, f"YouTrack: OK (project: {q})"
                return False, f"YouTrack: {_youtrack_error_detail(resp)}"

            test_url = f"{base_url}/api/admin/projects"
            resp = await client.get(test_url, headers=headers, params={"$top": 1})
            if resp.status_code == 200:
                return True, "YouTrack: авторизация OK (укажите Компонент)"
            return False, f"YouTrack: HTTP {resp.status_code}"
    except Exception as e:
        logger.exception("YouTrack test failed")
        return False, f"YouTrack: {e}"
