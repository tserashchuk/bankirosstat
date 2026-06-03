from __future__ import annotations

import imaplib
import logging
from typing import Any
from urllib.parse import quote

import caldav

logger = logging.getLogger(__name__)

# Официально: imap.yandex.com (документация Yandex Mail)
YANDEX_IMAP_HOSTS = ("imap.yandex.com", "imap.yandex.ru", "imap.ya.ru")
YANDEX_IMAP_PORT = 993
YANDEX_CALDAV_URL = "https://caldav.yandex.ru"

YANDEX_AUTH_HINT = (
    "Проверьте: 1) В Яндекс Почте включены «Почтовые клиенты» и IMAP "
    "(Настройки → Почтовые программы). "
    "2) Созданы отдельные пароли приложений: тип «Почта» для IMAP и тип «Календарь» для CalDAV "
    "(https://id.yandex.ru/security/app-passwords) — не основной пароль аккаунта. "
    "3) Пароль скопирован без пробелов."
)


def normalize_app_password(password: str) -> str:
    """Пароли приложений Yandex иногда вставляют с пробелами — убираем все пробелы."""
    return "".join(password.split())


def normalize_yandex_email(email: str) -> str:
    return email.strip().lower()


def yandex_login_variants(email: str) -> list[str]:
    email = normalize_yandex_email(email)
    variants = [email]
    if "@" in email:
        local = email.split("@", 1)[0]
        if local and local not in variants:
            variants.append(local)
    return variants


def yandex_imap_hosts(preferred: str | None = None) -> list[str]:
    hosts: list[str] = []
    if preferred:
        hosts.append(preferred)
    for h in YANDEX_IMAP_HOSTS:
        if h not in hosts:
            hosts.append(h)
    return hosts


def imap_login(
    conn: imaplib.IMAP4,
    email: str,
    password: str,
) -> str:
    """Пробует логин; возвращает успешный логин. Иначе — понятная ошибка."""
    password = normalize_app_password(password)
    last_err: Exception | None = None

    for login in yandex_login_variants(email):
        try:
            conn.login(login, password)
            return login
        except imaplib.IMAP4.error as e:
            last_err = e
            logger.debug("IMAP login as %s failed: %s", login, e)

    msg = str(last_err) if last_err else "unknown"
    if "AUTHENTICATIONFAILED" in msg.upper() or "INVALID" in msg.upper():
        raise ValueError(f"IMAP: неверный пароль или IMAP отключён (логин: {login})") from last_err
    raise ValueError(f"IMAP: {msg}") from last_err


def open_yandex_imap(
    email: str,
    password: str,
    *,
    preferred_host: str | None = None,
    port: int = YANDEX_IMAP_PORT,
) -> tuple[imaplib.IMAP4_SSL, str, str]:
    """Подключение к Yandex IMAP с перебором хостов и формата логина."""
    password = normalize_app_password(password)
    errors: list[str] = []

    for host in yandex_imap_hosts(preferred_host):
        try:
            conn = imaplib.IMAP4_SSL(host, port)
            login = imap_login(conn, email, password)
            return conn, host, login
        except Exception as e:
            errors.append(f"{host}: {e}")
            logger.debug("IMAP connect %s failed: %s", host, e)

    raise ValueError(
        "IMAP: не удалось войти ни на один сервер Yandex. "
        + "; ".join(errors[:2])
        + f". {YANDEX_AUTH_HINT}"
    )


def yandex_caldav_urls(email: str) -> list[str]:
    email = normalize_yandex_email(email)
    encoded = quote(email, safe="")
    return [
        YANDEX_CALDAV_URL,
        f"{YANDEX_CALDAV_URL}/principals/users/{encoded}/",
    ]


def open_yandex_caldav(email: str, password: str) -> tuple[caldav.DAVClient, Any, str]:
    """CalDAV Yandex: перебор URL. Возвращает (client, principal, url)."""
    password = normalize_app_password(password)
    email = normalize_yandex_email(email)
    errors: list[str] = []

    for url in yandex_caldav_urls(email):
        for login in yandex_login_variants(email):
            try:
                client = caldav.DAVClient(url=url, username=login, password=password)
                principal = client.principal()
                return client, principal, url
            except Exception as e:
                err_name = type(e).__name__
                errors.append(f"{url} ({login}): {err_name}")
                logger.debug("CalDAV %s as %s: %s", url, login, e)

    raise ValueError(
        f"CalDAV: авторизация не прошла. {YANDEX_AUTH_HINT}"
    )


def format_auth_error(service: str, err: Exception) -> str:
    text = str(err)
    upper = text.upper()
    if "AUTHENTICATION" in upper or "UNAUTHORIZED" in upper or "401" in upper:
        return f"{service}: ошибка авторизации. {YANDEX_AUTH_HINT}"
    return f"{service}: {text}"
