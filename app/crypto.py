from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet | None:
    key = get_settings().encryption_key
    if not key:
        return None
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_credentials(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False)
    f = _fernet()
    if f is None:
        return raw
    return f.encrypt(raw.encode()).decode()


def decrypt_credentials(stored: str) -> dict[str, Any]:
    f = _fernet()
    if f is None:
        return json.loads(stored)
    try:
        return json.loads(f.decrypt(stored.encode()).decode())
    except InvalidToken:
        raise ValueError(
            "Не удалось расшифровать сохранённый пароль. "
            "Проверьте ENCRYPTION_KEY в .env (тот же ключ, что при сохранении), "
            "затем заново введите пароли сотрудника."
        ) from None
    except json.JSONDecodeError:
        return json.loads(stored)
