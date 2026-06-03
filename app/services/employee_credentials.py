from __future__ import annotations

from typing import Any

from app.crypto import decrypt_credentials, encrypt_credentials
from app.services.collectors.yandex_auth import normalize_yandex_email


def rebind_yandex_email(encrypted: str, new_email: str) -> str:
    """Обновить email/login в сохранённых creds без смены пароля."""
    creds = decrypt_credentials(encrypted)
    email = normalize_yandex_email(new_email)
    if creds.get("email"):
        creds["email"] = email
    if creds.get("username"):
        creds["username"] = email
    return encrypt_credentials(creds)


def mail_creds_for_employee(encrypted: str | None, yandex_email: str) -> dict[str, Any] | None:
    if not encrypted:
        return None
    from app.services.source_creds import decrypt_normalized, prepare_email_creds

    creds = prepare_email_creds(decrypt_normalized(encrypted))
    if yandex_email:
        creds["email"] = normalize_yandex_email(yandex_email)
    return creds


def calendar_creds_for_employee(encrypted: str | None, yandex_email: str) -> dict[str, Any] | None:
    if not encrypted:
        return None
    from app.services.source_creds import decrypt_normalized

    creds = decrypt_normalized(encrypted)
    if yandex_email:
        creds["username"] = normalize_yandex_email(yandex_email)
        if not creds.get("email"):
            creds["email"] = creds["username"]
    return creds
