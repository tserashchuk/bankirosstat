from __future__ import annotations

import email
import imaplib
import logging
import re
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any

from app.services.collectors.collect_log import log_collect_block
from app.services.collectors.ical_utils import (
    COLLECTION_PERIOD_DAYS,
    dedupe_events,
    extract_meetings_from_message,
    week_window,
)
from app.services.source_creds import (
    decrypt_normalized,
    is_yandex_mailbox,
    prepare_email_creds,
    resolve_mail_password,
)
from app.services.collectors.yandex_auth import format_auth_error, imap_login, open_yandex_imap

logger = logging.getLogger(__name__)

_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MAX_MESSAGES = 200


def _imap_since_date(dt: datetime) -> str:
    """IMAP SINCE требует английские названия месяцев (не зависит от locale ОС)."""
    return f"{dt.day:02d}-{_IMAP_MONTHS[dt.month - 1]}-{dt.year}"


def _decode_mime(s: str | bytes | None) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        parts = decode_header(s)
        out = []
        for part, enc in parts:
            if isinstance(part, bytes):
                out.append(part.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(part)
        return "".join(out)
    return str(s)


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _body_snippet(msg: email.message.Message, max_len: int = 300) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
            elif ctype == "text/html" and not body:
                payload = part.get_payload(decode=True)
                if payload:
                    body = _strip_html(
                        payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    )
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            raw = payload.decode(charset, errors="replace")
            body = _strip_html(raw) if msg.get_content_type() == "text/html" else raw
    return body[:max_len].strip()


def _message_date(msg: email.message.Message) -> datetime | None:
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo:
            return dt.astimezone(tz=None).replace(tzinfo=None)
        return dt
    except (TypeError, ValueError, IndexError):
        return None


def _parse_message(msg: email.message.Message, start: datetime, end: datetime) -> tuple[dict[str, str], list[dict[str, Any]]]:
    subject = _decode_mime(msg.get("Subject"))
    meetings = extract_meetings_from_message(msg, start, end, mail_subject=subject)
    message = {
        "from": _decode_mime(msg.get("From")),
        "subject": subject,
        "date": _decode_mime(msg.get("Date")),
        "snippet": _body_snippet(msg),
    }
    if meetings:
        message["has_meeting_invite"] = True
    return message, meetings


def _search_message_ids(conn: imaplib.IMAP4, start: datetime, end: datetime) -> list[bytes]:
    """Только письма за окно [start, end]. Без fallback на ALL — не тянем всю почту."""
    since_str = _imap_since_date(start)
    # BEFORE — дата «следующего дня» после end (IMAP: строго до этой даты)
    before_str = _imap_since_date(end + timedelta(days=1))
    criteria_variants = (
        f'(SINCE "{since_str}" BEFORE "{before_str}")',
        f'SINCE "{since_str}"',
        f"SINCE {since_str}",
    )
    for criteria in criteria_variants:
        try:
            _, data = conn.search(None, criteria)
            if data and data[0]:
                ids = data[0].split()
                if ids:
                    return ids
        except Exception as e:
            logger.debug("IMAP search %s failed: %s", criteria, e)

    logger.warning(
        "IMAP: не удалось выполнить SINCE за %s дн. (с %s) — письма из папки не загружены",
        COLLECTION_PERIOD_DAYS,
        since_str,
    )
    return []


def _fetch_messages(
    conn: imaplib.IMAP4,
    ids: list[bytes],
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    messages: list[dict[str, str]] = []
    all_meetings: list[dict[str, Any]] = []

    for mid in ids[-MAX_MESSAGES:]:
        try:
            _, msg_data = conn.fetch(mid, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            msg = email.message_from_bytes(raw)
            msg_dt = _message_date(msg)
            if msg_dt and not (start <= msg_dt <= end):
                continue
            message, meetings = _parse_message(msg, start, end)
            messages.append(message)
            all_meetings.extend(meetings)
        except Exception as e:
            logger.debug("IMAP fetch %s failed: %s", mid, e)

    return messages, all_meetings


def collect_email_from_creds(creds: dict[str, Any]) -> dict[str, Any]:
    creds = prepare_email_creds(creds)
    host = creds.get("imap_server")
    if not host:
        raise ValueError("Не указан IMAP сервер")
    port = int(creds.get("port") or 993)
    user = creds.get("email")
    if not user:
        raise ValueError("Не указан email")
    password = resolve_mail_password(creds)
    if not password:
        raise ValueError("Не указан пароль почты")
    use_ssl = creds.get("ssl", True)

    start, end = week_window()
    logger.info(
        "Email collect start | user=%s host=%s period=%s..%s",
        user,
        host,
        start.date().isoformat(),
        end.date().isoformat(),
    )

    messages: list[dict[str, str]] = []
    all_meetings: list[dict[str, Any]] = []
    folders_tried: list[str] = []

    is_yandex = creds.get("provider") == "yandex" or is_yandex_mailbox(user)
    conn: imaplib.IMAP4 | imaplib.IMAP4_SSL | None = None

    try:
        if is_yandex and use_ssl:
            conn, host, _login = open_yandex_imap(user, password, preferred_host=host, port=port)
        elif use_ssl:
            conn = imaplib.IMAP4_SSL(host, port)
            imap_login(conn, user, password)
        else:
            conn = imaplib.IMAP4(host, port)
            imap_login(conn, user, password)
        for folder in ("INBOX", "Inbox", "Входящие"):
            try:
                status, _ = conn.select(folder, readonly=True)
                if status != "OK":
                    continue
                folders_tried.append(folder)
                ids = _search_message_ids(conn, start, end)
                msgs, meets = _fetch_messages(conn, ids, start, end)
                messages.extend(msgs)
                all_meetings.extend(meets)
                logger.info(
                    "Email IMAP folder %s: ids=%s fetched_msgs=%s",
                    folder,
                    len(ids),
                    len(msgs),
                )
                if messages:
                    break
            except Exception as e:
                logger.warning("Email IMAP folder %s failed: %s", folder, e)

        if not folders_tried:
            raise ValueError("Не удалось открыть папку входящих (INBOX)")
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    meetings = dedupe_events(all_meetings)

    result = {
        "source": "email",
        "mailbox": user,
        "collected_at": datetime.utcnow().isoformat(),
        "period_days": COLLECTION_PERIOD_DAYS,
        "period_from": start.isoformat(),
        "period_to": end.isoformat(),
        "messages_count": len(messages),
        "meetings_count": len(meetings),
        "messages": messages,
        "meetings": meetings,
    }
    log_collect_block(result)
    return result


def collect_email(credentials_encrypted: str) -> dict[str, Any]:
    return collect_email_from_creds(decrypt_normalized(credentials_encrypted))


def test_email_from_creds(creds: dict[str, Any]) -> tuple[bool, str]:
    from app.models import SourceType
    from app.services.source_creds import missing_labels

    creds = prepare_email_creds(creds)
    missing = missing_labels(SourceType.EMAIL, creds)
    if missing:
        return False, f"Почта: для проверки укажите {missing}"
    try:
        data = collect_email_from_creds(creds)
        n_msg = data.get("messages_count", len(data.get("messages") or []))
        n_meetings = data.get("meetings_count", len(data.get("meetings") or []))
        login = creds.get("email", "?")
        base = f"Почта ({login}): соединение успешно, писем за неделю: {n_msg}"
        if n_meetings:
            return True, f"{base}, встреч из приглашений: {n_meetings}"
        return True, base
    except Exception as e:
        return False, format_auth_error("Почта", e)


def test_email_connection(credentials_encrypted: str) -> tuple[bool, str]:
    from app.models import SourceType
    from app.services.source_creds import missing_labels

    return test_email_from_creds(decrypt_normalized(credentials_encrypted))
