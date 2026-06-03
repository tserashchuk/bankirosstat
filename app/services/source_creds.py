from __future__ import annotations

from typing import Any

from app.crypto import decrypt_credentials
from app.models import SourceType

# Минимум полей для реального сбора данных (не для сохранения источника).
COLLECT_REQUIRED: dict[SourceType, tuple[str, ...]] = {
    SourceType.YOUTRACK: ("url", "token"),
    SourceType.EMAIL: ("imap_server", "email"),
    SourceType.CALENDAR: ("url",),
    SourceType.GOOGLE_DOC: ("document_id",),
    SourceType.GOOGLE_SHEET: ("spreadsheet_id",),
}

FIELD_LABELS: dict[str, str] = {
    "url": "URL",
    "token": "Token",
    "project_id": "Project ID",
    "component_name": "Компонент",
    "board_name": "Board Name (устар.)",
    "board_id": "Board ID (устар.)",
    "imap_server": "IMAP сервер",
    "email": "Email",
    "password": "Пароль",
    "mail_password": "Пароль почты",
    "calendar_password": "Пароль календаря",
    "document_id": "ID документа",
    "spreadsheet_id": "ID таблицы",
}


def normalize_credentials(creds: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in creds.items():
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                out[key] = stripped
        elif isinstance(value, bool):
            out[key] = value
        elif value != "":
            out[key] = value
    return out


def decrypt_normalized(credentials_encrypted: str) -> dict[str, Any]:
    return normalize_credentials(decrypt_credentials(credentials_encrypted))


YANDEX_IMAP_HOST = "imap.yandex.com"
YANDEX_IMAP_PORT = 993


def is_yandex_mailbox(email: str) -> bool:
    e = (email or "").lower()
    return any(
        d in e
        for d in ("@yandex.ru", "@yandex.com", "@ya.ru", "@yandex.by", "@yandex.kz", "@yandex.ua")
    )


def prepare_email_creds(creds: dict[str, Any]) -> dict[str, Any]:
    """Подставляет IMAP-настройки Yandex, если указан только email и пароль."""
    out = dict(creds)
    email = out.get("email", "")
    if not out.get("imap_server") and (
        out.get("provider") == "yandex" or is_yandex_mailbox(email)
    ):
        out["imap_server"] = YANDEX_IMAP_HOST
        out.setdefault("port", YANDEX_IMAP_PORT)
        out.setdefault("ssl", True)
        out.setdefault("provider", "yandex")
    if out.get("port"):
        try:
            out["port"] = int(out["port"])
        except (TypeError, ValueError):
            out["port"] = YANDEX_IMAP_PORT
    return out


def resolve_mail_password(creds: dict[str, Any]) -> str:
    from app.services.collectors.yandex_auth import normalize_app_password

    raw = str(
        creds.get("mail_password") or creds.get("password") or creds.get("app_password") or ""
    )
    return normalize_app_password(raw) if raw else ""


def resolve_calendar_password(creds: dict[str, Any]) -> str:
    from app.services.collectors.yandex_auth import normalize_app_password

    raw = str(creds.get("calendar_password") or creds.get("password") or "")
    return normalize_app_password(raw) if raw else ""


def missing_for_collect(source_type: SourceType, creds: dict[str, Any]) -> list[str]:
    if source_type == SourceType.EMAIL:
        creds = prepare_email_creds(creds)
    if source_type == SourceType.YOUTRACK:
        missing: list[str] = []
        if not creds.get("url"):
            missing.append("url")
        if not creds.get("token"):
            missing.append("token")
        has_component = bool(creds.get("component_name"))
        if not has_component and not creds.get("project_id"):
            missing.append("component_name")
        return missing
    required = COLLECT_REQUIRED.get(source_type, ())
    missing = [f for f in required if not creds.get(f)]
    if source_type == SourceType.EMAIL and not resolve_mail_password(creds):
        missing.append("mail_password")
    if source_type == SourceType.CALENDAR:
        url = creds.get("url") or ""
        needs_auth = "caldav" in url.lower() or creds.get("protocol") == "caldav"
        if needs_auth and not resolve_calendar_password(creds):
            missing.append("calendar_password")
    return missing


def can_collect(source_type: SourceType, creds: dict[str, Any]) -> bool:
    return not missing_for_collect(source_type, creds)


def missing_labels(source_type: SourceType, creds: dict[str, Any]) -> str:
    keys = missing_for_collect(source_type, creds)
    if not keys:
        return ""
    labels = [FIELD_LABELS.get(k, k) for k in keys]
    return ", ".join(labels)
