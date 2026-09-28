from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from typing import Any, Optional

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


def redis_settings() -> RedisSettings:
    """Конфигурация Redis для ARQ из REDIS_URL."""
    # Docker DNS на старте контейнера иногда отвечает EAI_AGAIN —
    # без запаса ретраев app поднимается без очереди и так и остаётся.
    return replace(
        RedisSettings.from_dsn(settings.redis_url),
        conn_timeout=2,
        conn_retries=15,
        conn_retry_delay=1,
    )


async def create_arq_pool() -> ArqRedis:
    """Создаёт пул соединений к Redis для постановки задач из web-процесса."""
    pool = await create_pool(redis_settings())
    logger.info("ARQ pool connected to %s", settings.redis_url)
    return pool


async def close_arq_pool(pool: Optional[ArqRedis]) -> None:
    if pool is None:
        return
    try:
        await pool.aclose()
    except AttributeError:
        await pool.close()


async def enqueue(
    pool: ArqRedis,
    function_name: str,
    job_id: uuid.UUID | str,
    *args: Any,
) -> None:
    """Ставит задачу в очередь ARQ. job_id используется и как _job_id, и как первый аргумент функции."""
    jid = str(job_id)
    arq_job = await pool.enqueue_job(function_name, jid, *args, _job_id=f"job:{jid}")
    if arq_job is None:
        raise RuntimeError(
            f"Не удалось поставить задачу '{function_name}' (job_id={jid}) — "
            "возможно, такой job_id уже в очереди"
        )
    logger.info("Enqueued %s job_id=%s", function_name, jid)
