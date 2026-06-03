"""Entrypoint для ARQ-воркера.

Запуск:
    arq app.services.jobs.worker.WorkerSettings
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import get_settings
from app.database import init_db
from app.logging_config import configure_logging
from app.services.jobs.queue import redis_settings
from app.services.jobs.tasks import JOB_FUNCTIONS

logger = logging.getLogger(__name__)


async def _startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    init_db()
    logger.info("ARQ worker started, %s functions registered", len(JOB_FUNCTIONS))


async def _shutdown(ctx: dict[str, Any]) -> None:
    logger.info("ARQ worker shutting down")


class WorkerSettings:
    """Конфигурация ARQ-воркера. Подбирает таймауты/конкуренцию из .env."""

    _settings = get_settings()

    functions = JOB_FUNCTIONS
    redis_settings = redis_settings()
    on_startup = _startup
    on_shutdown = _shutdown
    job_timeout = _settings.jobs_worker_timeout
    max_jobs = _settings.jobs_worker_concurrency
    keep_result = 60 * 60  # 1 час: результат держится в Redis для дебага
    max_tries = 1  # тяжёлые задачи; повторы пусть запускаются вручную
