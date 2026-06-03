from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import caldav
import httpx
from icalendar import Calendar

from app.services.collectors.ical_utils import (
    COLLECTION_PERIOD_DAYS,
    annotate_participant_events,
    cap_events,
    dedupe_events,
    filter_events_in_window,
    merge_participant_meetings,
    parse_events_from_ical,
    week_window,
    _vevent_to_dict,
)
from app.services.source_creds import (
    decrypt_normalized,
    is_yandex_mailbox,
    resolve_calendar_password,
)
from app.services.collectors.collect_log import log_collect_block
from app.services.collectors.yandex_auth import format_auth_error, open_yandex_caldav

logger = logging.getLogger(__name__)

MAX_CALENDAR_EVENTS = 150


def _fetch_ical(url: str, username: str | None = None, password: str | None = None) -> Calendar:
    auth = (username, password) if username and password else None
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        resp = client.get(url, auth=auth)
        resp.raise_for_status()
        data = resp.content
    return Calendar.from_ical(data)


def collect_calendar_from_creds(
    creds: dict[str, Any],
    *,
    participant_email: str | None = None,
    trust_own_calendar: bool = False,
    extra_meetings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    url = creds.get("url") or creds.get("caldav_url")
    username = creds.get("username")
    password = resolve_calendar_password(creds) or None
    label = creds.get("label", url)

    start, end = week_window()
    all_events: list[dict[str, Any]] = []

    is_yandex = creds.get("provider") == "yandex" or is_yandex_mailbox(username or "")
    use_caldav = creds.get("protocol") == "caldav" or "caldav" in (url or "").lower()

    logger.info(
        "Calendar collect start | label=%r mode=%s user=%s period=%s..%s",
        label,
        "caldav" if use_caldav else "ical",
        username or "?",
        start.date().isoformat(),
        end.date().isoformat(),
    )

    if use_caldav:
        if is_yandex and password and username:
            _client, principal, caldav_url = open_yandex_caldav(username, password)
            logger.info("Calendar CalDAV Yandex OK | user=%s url=%s", username, caldav_url)
        else:
            client = caldav.DAVClient(url=url, username=username, password=password)
            principal = client.principal()
            logger.info("Calendar CalDAV OK | url=%s user=%s", url, username or "?")
        calendars = principal.calendars()
        logger.info("Calendar CalDAV: найдено %s календарей", len(calendars))
        for cal in calendars:
            cal_name = getattr(cal, "name", None) or str(cal)
            try:
                found = cal.date_search(start, end, expand=True)
            except Exception as e:
                logger.warning("CalDAV date_search failed | %s | %s: %s", label, cal_name, e)
                continue
            logger.info(
                "Calendar CalDAV date_search | %s | %s: %s событий",
                label,
                cal_name,
                len(found),
            )
            n_before = len(all_events)
            for ev in found:
                parsed: list[dict[str, Any]] = []
                try:
                    comp = ev.icalendar_component
                    one = _vevent_to_dict(comp, start, end)
                    if one:
                        one["calendar_name"] = cal_name
                        parsed.append(one)
                except Exception:
                    pass
                if not parsed:
                    parsed = parse_events_from_ical(ev.data, start, end)
                for row in parsed:
                    row.setdefault("calendar_name", cal_name)
                all_events.extend(parsed)
            logger.info(
                "Calendar CalDAV %s | %s: после разбора iCal/RRULE +%s событий",
                label,
                cal_name,
                len(all_events) - n_before,
            )
        before_dedupe = len(all_events)
        all_events = dedupe_events(all_events)
        if before_dedupe != len(all_events):
            logger.info(
                "Calendar %s: dedupe %s → %s событий",
                label,
                before_dedupe,
                len(all_events),
            )
    else:
        logger.info("Calendar iCal GET | url=%s", url)
        cal = _fetch_ical(url, username, password)
        all_events = parse_events_from_ical(cal.to_ical(), start, end)

    before_filter = len(all_events)
    all_events = filter_events_in_window(all_events, start, end)
    if before_filter != len(all_events):
        logger.info(
            "Calendar %s: filter window %s → %s событий (%s..%s)",
            label,
            before_filter,
            len(all_events),
            start.date().isoformat(),
            end.date().isoformat(),
        )
    if participant_email:
        before_part = len(all_events)
        all_events = annotate_participant_events(
            all_events,
            participant_email,
            trust_own_calendar=trust_own_calendar,
        )
        if before_part != len(all_events):
            logger.info(
                "Calendar %s: участие %s — %s → %s встреч",
                label,
                participant_email,
                before_part,
                len(all_events),
            )

    if extra_meetings and participant_email:
        before_merge = len(all_events)
        all_events = merge_participant_meetings(
            all_events,
            extra_meetings,
            participant_email,
            trust_own_calendar=trust_own_calendar,
        )
        logger.info(
            "Calendar %s: +invite %s встреч (всего %s)",
            label,
            len(all_events) - before_merge,
            len(all_events),
        )

    if len(all_events) > MAX_CALENDAR_EVENTS:
        dropped = len(all_events) - MAX_CALENDAR_EVENTS
        all_events = cap_events(all_events, MAX_CALENDAR_EVENTS)
        logger.info(
            "Calendar %s: оставлено %s событий за %s дн., отброшено %s",
            label,
            MAX_CALENDAR_EVENTS,
            COLLECTION_PERIOD_DAYS,
            dropped,
        )

    result = {
        "source": "calendar",
        "label": label,
        "username": username,
        "protocol": "caldav" if use_caldav else "ical",
        "provider": creds.get("provider"),
        "collected_at": datetime.utcnow().isoformat(),
        "period_days": COLLECTION_PERIOD_DAYS,
        "period_from": start.isoformat(),
        "period_to": end.isoformat(),
        "events_count": len(all_events),
        "events": all_events,
        "participant_email": participant_email,
    }
    log_collect_block(result, context=label)
    return result


def collect_calendar(credentials_encrypted: str) -> dict[str, Any]:
    return collect_calendar_from_creds(decrypt_normalized(credentials_encrypted))


def test_calendar_from_creds(creds: dict[str, Any]) -> tuple[bool, str]:
    from app.models import SourceType
    from app.services.source_creds import missing_labels

    missing = missing_labels(SourceType.CALENDAR, creds)
    if missing:
        return False, f"Календарь: для проверки укажите {missing}"
    try:
        data = collect_calendar_from_creds(creds)
        login = creds.get("username") or creds.get("email") or "?"
        n = data.get("events_count", len(data.get("events") or []))
        days = data.get("period_days", COLLECTION_PERIOD_DAYS)
        return True, f"Календарь ({login}): {n} событий за {days} дн."
    except Exception as e:
        return False, format_auth_error("Календарь", e)


def test_calendar_connection(credentials_encrypted: str) -> tuple[bool, str]:
    from app.models import SourceType
    from app.services.source_creds import missing_labels

    return test_calendar_from_creds(decrypt_normalized(credentials_encrypted))
