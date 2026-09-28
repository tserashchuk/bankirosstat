from __future__ import annotations

import logging
from typing import Optional

from arq.connections import ArqRedis

from app.services.jobs.store import (
    JOB_CANCELLED_MESSAGE,
    finish_job_error,
    list_cancellable_jobs,
)

logger = logging.getLogger(__name__)


async def cancel_all_active_jobs(pool: Optional[ArqRedis]) -> list[str]:
    """Отменяет все PENDING/RUNNING задачи в БД и снимает их с очереди ARQ."""
    jobs = list_cancellable_jobs()
    cancelled_ids: list[str] = []

    for job in jobs:
        job_id = str(job.id)
        finish_job_error(job_id, JOB_CANCELLED_MESSAGE)
        cancelled_ids.append(job_id)

        if pool is None:
            continue
        try:
            await pool.abort_job(f"job:{job_id}")
        except Exception as exc:
            logger.warning("abort_job failed for %s: %s", job_id, exc)

    if cancelled_ids:
        logger.info("Cancelled %d background job(s)", len(cancelled_ids))

    return cancelled_ids
