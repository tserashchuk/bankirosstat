from __future__ import annotations

import email
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from icalendar import Calendar

_MAILTO_RE = re.compile(r"mailto:([^;\s>?]+)", re.I)

CALENDAR_CONTENT_TYPES = frozenset(
    {
        "text/calendar",
        "application/ics",
        "application/icalendar",
        "application/x-ical",
    }
)


def _to_naive_utc(dt: datetime | date) -> datetime:
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return datetime.combine(dt, datetime.min.time())
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def extract_email_from_ical_address(value: str) -> str | None:
    """mailto:user@domain из ORGANIZER/ATTENDEE."""
    if not value:
        return None
    text = str(value).strip()
    match = _MAILTO_RE.search(text)
    if match:
        return match.group(1).strip().lower()
    if "@" in text:
        return text.lower()
    return None


def participant_email_variants(email: str) -> set[str]:
    """Варианты для сопоставления участия (pm_mob@bankiros.ru, pm_mob, …)."""
    normalized = (email or "").strip().lower()
    if not normalized:
        return set()
    variants = {normalized}
    if "@" in normalized:
        local = normalized.split("@", 1)[0]
        if local:
            variants.add(local)
    return variants


def _event_emails(event: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    org = extract_email_from_ical_address(str(event.get("organizer") or ""))
    if org:
        found.add(org)
    for raw in event.get("attendees") or []:
        addr = extract_email_from_ical_address(str(raw))
        if addr:
            found.add(addr)
    return found


def event_participation_role(
    event: dict[str, Any], participant_emails: set[str]
) -> str | None:
    """organizer | attendee | None."""
    if not participant_emails:
        return None
    org = extract_email_from_ical_address(str(event.get("organizer") or ""))
    if org and org in participant_emails:
        return "organizer"
    if _event_emails(event) & participant_emails:
        return "attendee"
    return None


def event_involves_participant(
    event: dict[str, Any],
    participant_emails: set[str],
    *,
    trust_own_calendar: bool = False,
) -> bool:
    """
    Встреча с участием сотрудника: ORGANIZER/ATTENDEE или событие из личного CalDAV.
    """
    if not participant_emails:
        return True
    role = event_participation_role(event, participant_emails)
    if role:
        return True
    if trust_own_calendar:
        # Событие в календаре сотрудника без явных участников — считаем его встречей.
        return True
    return False


def annotate_participant_events(
    events: list[dict[str, Any]],
    participant_email: str | None,
    *,
    trust_own_calendar: bool = False,
) -> list[dict[str, Any]]:
    variants = participant_email_variants(participant_email or "")
    out: list[dict[str, Any]] = []
    for ev in events:
        row = dict(ev)
        if variants:
            role = event_participation_role(row, variants)
            if role:
                row["participation"] = role
            elif trust_own_calendar:
                row["participation"] = "calendar"
            elif not event_involves_participant(row, variants, trust_own_calendar=False):
                continue
        out.append(row)
    return out


def merge_participant_meetings(
    calendar_events: list[dict[str, Any]],
    extra_meetings: list[dict[str, Any]],
    participant_email: str,
    *,
    trust_own_calendar: bool = False,
) -> list[dict[str, Any]]:
    """Объединяет CalDAV и встречи из почтовых invite."""
    variants = participant_email_variants(participant_email)
    merged = list(calendar_events)
    seen = {f"{e.get('uid')}|{e.get('start')}" for e in merged if e.get("uid")}
    for m in extra_meetings:
        if not event_involves_participant(m, variants, trust_own_calendar=False):
            continue
        key = f"{m.get('uid')}|{m.get('start')}" if m.get("uid") else ""
        if key and key in seen:
            continue
        row = dict(m)
        role = event_participation_role(row, variants)
        if role:
            row["participation"] = role
        else:
            row["participation"] = "invite"
        row["from_email_invite"] = True
        merged.append(row)
        if key:
            seen.add(key)
    return dedupe_events(merged)


def _event_overlaps_window(
    dt_start: datetime,
    dt_end: datetime,
    start: datetime,
    end: datetime,
) -> bool:
    return not (dt_end < start or dt_start > end)


def _vevent_to_dict(
    component: Any,
    start: datetime,
    end: datetime,
) -> dict[str, Any] | None:
    """Все VEVENT в окне — рабочий календарь, без фильтра CLASS=PRIVATE."""
    if component.name != "VEVENT":
        return None

    status = str(component.get("STATUS") or "").upper()
    if status == "CANCELLED":
        return None

    dtstart = component.get("DTSTART")
    if not dtstart:
        return None

    dt = _to_naive_utc(dtstart.dt)
    dtend_raw = component.get("DTEND")
    dt_end = _to_naive_utc(dtend_raw.dt) if dtend_raw else dt
    if not _event_overlaps_window(dt, dt_end, start, end):
        return None

    attendees: list[str] = []
    att = component.get("ATTENDEE")
    if att:
        if not isinstance(att, list):
            att = [att]
        for a in att:
            attendees.append(str(a))

    uid = str(component.get("UID") or "")
    item: dict[str, Any] = {
        "summary": str(component.get("SUMMARY") or ""),
        "start": dt.isoformat(),
        "end": (
            _to_naive_utc(component.get("DTEND").dt).isoformat()
            if component.get("DTEND")
            else None
        ),
        "location": str(component.get("LOCATION") or "") or None,
        "organizer": str(component.get("ORGANIZER") or "") or None,
        "attendees": attendees,
    }
    if uid:
        item["uid"] = uid
    return item


def _expand_recurring_events(
    cal: Calendar,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Разворачивает RRULE (Дейли и др.) — иначе остаётся только master DTSTART."""
    try:
        import recurring_ical_events
    except ImportError:
        return []

    events: list[dict[str, Any]] = []
    try:
        occurrences = recurring_ical_events.of(cal).between(start, end)
    except Exception:
        return events

    for occ in occurrences:
        if getattr(occ, "name", None) != "VEVENT":
            continue
        item = _vevent_to_dict(occ, start, end)
        if item:
            events.append(item)
    return events


def parse_events_from_ical(
    raw: bytes,
    start: datetime,
    end: datetime,
    *,
    origin: str | None = None,
) -> list[dict[str, Any]]:
    """Parse VEVENT components from iCalendar data within [start, end]."""
    events: list[dict[str, Any]] = []
    try:
        cal = Calendar.from_ical(raw)
    except Exception:
        return events

    seen: set[str] = set()

    def _add(item: dict[str, Any] | None) -> None:
        if not item:
            return
        key = f"{item.get('uid')}|{item.get('start')}" if item.get("uid") else f"{item.get('summary')}|{item.get('start')}"
        if key in seen:
            return
        seen.add(key)
        if origin:
            item["from_email"] = origin
        events.append(item)

    for item in _expand_recurring_events(cal, start, end):
        _add(item)

    for component in cal.walk():
        _add(_vevent_to_dict(component, start, end))

    return events


def iter_calendar_payloads(msg: email.message.Message) -> list[bytes]:
    """Extract iCalendar bytes from MIME parts (inline or .ics attachment)."""
    payloads: list[bytes] = []

    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            filename = (part.get_filename() or "").lower()
            disp = str(part.get("Content-Disposition", "")).lower()

            is_calendar = ctype in CALENDAR_CONTENT_TYPES
            is_ics_file = filename.endswith(".ics") or filename.endswith(".ical")

            if not is_calendar and not is_ics_file:
                continue

            raw = part.get_payload(decode=True)
            if raw:
                payloads.append(raw)
    else:
        ctype = (msg.get_content_type() or "").lower()
        if ctype in CALENDAR_CONTENT_TYPES:
            raw = msg.get_payload(decode=True)
            if raw:
                payloads.append(raw)

    return payloads


def extract_meetings_from_message(
    msg: email.message.Message,
    start: datetime,
    end: datetime,
    *,
    mail_subject: str = "",
) -> list[dict[str, Any]]:
    origin = mail_subject[:200] if mail_subject else None
    meetings: list[dict[str, Any]] = []
    for raw in iter_calendar_payloads(msg):
        meetings.extend(parse_events_from_ical(raw, start, end, origin=origin))
    return meetings


def dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for ev in events:
        uid = ev.get("uid") or ""
        start = ev.get("start") or ""
        key = f"{uid}|{start}" if uid else f"{ev.get('summary')}|{start}"
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    return out


COLLECTION_PERIOD_DAYS = 7


def week_window() -> tuple[datetime, datetime]:
    """Окно: COLLECTION_PERIOD_DAYS полных календарных дней UTC + остаток сегодняшнего дня."""
    now = datetime.utcnow()
    start_date = (now - timedelta(days=COLLECTION_PERIOD_DAYS)).date()
    start = datetime.combine(start_date, datetime.min.time())
    end = datetime.combine(now.date(), datetime.max.time()).replace(microsecond=0)
    return start, end


def _parse_event_bounds(ev: dict[str, Any]) -> tuple[datetime | None, datetime | None]:
    raw_start = ev.get("start")
    if not raw_start:
        return None, None
    try:
        dt_start = datetime.fromisoformat(str(raw_start).replace("Z", "+00:00"))
        if dt_start.tzinfo:
            dt_start = dt_start.astimezone(timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None, None

    raw_end = ev.get("end")
    if raw_end:
        try:
            dt_end = datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
            if dt_end.tzinfo:
                dt_end = dt_end.astimezone(timezone.utc).replace(tzinfo=None)
        except (TypeError, ValueError):
            dt_end = dt_start
    else:
        dt_end = dt_start
    return dt_start, dt_end


def filter_events_in_window(
    events: list[dict[str, Any]],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Событие попадает, если пересекает [start, end] (как в parse_events_from_ical)."""
    out: list[dict[str, Any]] = []
    for ev in events:
        dt_start, dt_end = _parse_event_bounds(ev)
        if dt_start is None:
            continue
        if dt_end < start or dt_start > end:
            continue
        out.append(ev)
    return out


def cap_events(events: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(events) <= limit:
        return events
    return sorted(events, key=lambda e: e.get("start") or "", reverse=True)[:limit]
