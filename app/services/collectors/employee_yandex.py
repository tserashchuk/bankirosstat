from __future__ import annotations

import logging
from typing import Any

from app.models import Employee
from app.services.collectors.collect_log import log_collect_block
from app.services.collectors.calendar import (
    collect_calendar_from_creds,
    test_calendar_from_creds,
)
from app.services.collectors.email_imap import collect_email_from_creds, test_email_from_creds
from app.services.employee_credentials import calendar_creds_for_employee, mail_creds_for_employee
from app.services.collectors.yandex_auth import (
    YANDEX_CALDAV_URL,
    YANDEX_IMAP_HOSTS,
    YANDEX_IMAP_PORT,
    normalize_app_password,
    normalize_yandex_email,
)

YANDEX_IMAP_SERVER = YANDEX_IMAP_HOSTS[0]

logger = logging.getLogger(__name__)


def build_yandex_mail_credentials(email: str, mail_password: str) -> dict[str, Any]:
    return {
        "imap_server": YANDEX_IMAP_SERVER,
        "port": YANDEX_IMAP_PORT,
        "email": normalize_yandex_email(email),
        "mail_password": normalize_app_password(mail_password),
        "ssl": True,
        "provider": "yandex",
    }


def build_yandex_calendar_credentials(email: str, calendar_password: str) -> dict[str, Any]:
    return {
        "url": YANDEX_CALDAV_URL,
        "username": normalize_yandex_email(email),
        "calendar_password": normalize_app_password(calendar_password),
        "protocol": "caldav",
        "provider": "yandex",
    }


# Обратная совместимость
build_yandex_credentials = build_yandex_mail_credentials


def employee_mail_credentials_encrypted(employee: Employee) -> str | None:
    return employee.imap_password_encrypted


def employee_calendar_credentials_encrypted(employee: Employee) -> str | None:
    return getattr(employee, "calendar_password_encrypted", None)


def collect_employee_yandex_mail(employee: Employee) -> dict[str, Any]:
    enc = employee_mail_credentials_encrypted(employee)
    if not enc:
        skipped = {
            "source": "employee_email",
            "employee": employee.full_name,
            "mailbox": employee.yandex_email,
            "skipped": True,
            "reason": "Не задан пароль почты Yandex",
            "messages": [],
            "meetings": [],
        }
        log_collect_block(skipped, context=employee.full_name or employee.yandex_email)
        return skipped
    creds = mail_creds_for_employee(enc, employee.yandex_email)
    if not creds:
        raise ValueError("Не удалось прочитать настройки почты")
    data = collect_email_from_creds(creds)
    data["source"] = "employee_email"
    data["employee"] = employee.full_name
    data["employee_id"] = str(employee.id)
    data["provider"] = "yandex"
    log_collect_block(data, context=employee.full_name or employee.yandex_email)
    return data


def _employee_invite_meetings(employee: Employee) -> list[dict[str, Any]]:
    """Встречи из писем с .ics, где сотрудник в участниках."""
    enc = employee_mail_credentials_encrypted(employee)
    if not enc:
        return []
    creds = mail_creds_for_employee(enc, employee.yandex_email)
    if not creds:
        return []
    try:
        mail_data = collect_email_from_creds(creds)
        return list(mail_data.get("meetings") or [])
    except Exception as e:
        logger.warning(
            "Employee %s: встречи из почты не загружены: %s",
            employee.yandex_email,
            e,
        )
        return []


def collect_employee_yandex_calendar(employee: Employee) -> dict[str, Any]:
    enc = employee_calendar_credentials_encrypted(employee)
    if not enc:
        skipped = {
            "source": "employee_calendar",
            "employee": employee.full_name,
            "skipped": True,
            "reason": "Не задан пароль календаря Yandex",
            "events": [],
        }
        log_collect_block(skipped, context=employee.full_name or employee.yandex_email)
        return skipped
    creds = calendar_creds_for_employee(enc, employee.yandex_email)
    if not creds:
        raise ValueError("Не удалось прочитать настройки календаря")
    email = employee.yandex_email or creds.get("username") or ""
    invite_meetings = _employee_invite_meetings(employee)
    if invite_meetings:
        logger.info(
            "Employee %s: встреч из почтовых invite %s",
            email,
            len(invite_meetings),
        )
    data = collect_calendar_from_creds(
        creds,
        participant_email=email,
        trust_own_calendar=True,
        extra_meetings=invite_meetings,
    )
    data["source"] = "employee_calendar"
    data["employee"] = employee.full_name
    data["employee_id"] = str(employee.id)
    data["employee_email"] = email
    data["provider"] = "yandex"
    data["invite_meetings_merged"] = len(invite_meetings)
    log_collect_block(data, context=employee.full_name or employee.yandex_email)
    return data


def test_employee_yandex_mail(employee: Employee) -> tuple[bool, str]:
    enc = employee_mail_credentials_encrypted(employee)
    if not enc:
        return False, "Почта: не задан пароль"
    creds = mail_creds_for_employee(enc, employee.yandex_email)
    if not creds:
        return False, "Почта: не удалось прочитать сохранённый пароль"
    return test_email_from_creds(creds)


def test_employee_yandex_calendar(employee: Employee) -> tuple[bool, str]:
    enc = employee_calendar_credentials_encrypted(employee)
    if not enc:
        return False, "Календарь: не задан пароль"
    creds = calendar_creds_for_employee(enc, employee.yandex_email)
    if not creds:
        return False, "Календарь: не удалось прочитать сохранённый пароль"
    return test_calendar_from_creds(creds)


def test_employee_yandex_connections(employee: Employee) -> tuple[bool, str]:
    lines: list[str] = []
    ok_all = True
    has_any = False

    if employee_mail_credentials_encrypted(employee):
        has_any = True
        ok, msg = test_employee_yandex_mail(employee)
        lines.append(msg)
        ok_all = ok_all and ok

    if employee_calendar_credentials_encrypted(employee):
        has_any = True
        ok, msg = test_employee_yandex_calendar(employee)
        lines.append(msg)
        ok_all = ok_all and ok

    if not has_any:
        return False, "Укажите пароль почты и/или пароль календаря"
    return ok_all, "\n".join(lines)
