"""Обратная совместимость — логика в employee_yandex."""
from app.services.collectors.employee_yandex import (
    build_yandex_credentials,
    build_yandex_mail_credentials,
    collect_employee_yandex_mail,
    test_employee_yandex_connections,
    test_employee_yandex_mail,
)

__all__ = [
    "build_yandex_credentials",
    "build_yandex_mail_credentials",
    "collect_employee_yandex_mail",
    "test_employee_yandex_connections",
    "test_employee_yandex_mail",
]
