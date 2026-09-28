from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlmodel import Session, select

from app.database import engine
from app.models import (
    BackgroundJob,
    BackgroundJobStatus,
    BackgroundJobType,
)

logger = logging.getLogger(__name__)

JOB_CANCELLED_MESSAGE = "Отменено пользователем"

_TERMINAL_STATUSES = frozenset(
    {BackgroundJobStatus.DONE, BackgroundJobStatus.ERROR}
)


class JobCancelledError(Exception):
    """Задача отменена или уже завершена."""


def _now() -> datetime:
    return datetime.utcnow()


def create_job(
    job_type: BackgroundJobType,
    *,
    title: str = "",
    project_id: Optional[uuid.UUID] = None,
    payload: Optional[dict[str, Any]] = None,
    progress: str = "В очереди...",
) -> BackgroundJob:
    """Создаёт запись о задаче в БД (статус PENDING)."""
    job = BackgroundJob(
        type=job_type,
        status=BackgroundJobStatus.PENDING,
        title=title or job_type.value,
        progress=progress,
        project_id=project_id,
        payload=payload or {},
    )
    with Session(engine) as session:
        session.add(job)
        session.commit()
        session.refresh(job)
    return job


def get_job(job_id: uuid.UUID | str) -> Optional[BackgroundJob]:
    if isinstance(job_id, str):
        try:
            job_id = uuid.UUID(job_id)
        except ValueError:
            return None
    with Session(engine) as session:
        return session.get(BackgroundJob, job_id)


def _update(job_id: uuid.UUID | str, **fields: Any) -> Optional[BackgroundJob]:
    if isinstance(job_id, str):
        try:
            job_id = uuid.UUID(job_id)
        except ValueError:
            return None
    with Session(engine) as session:
        job = session.get(BackgroundJob, job_id)
        if not job:
            return None
        for key, value in fields.items():
            setattr(job, key, value)
        job.updated_at = _now()
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


def mark_running(job_id: uuid.UUID | str, progress: str = "Старт...") -> Optional[BackgroundJob]:
    job = get_job(job_id)
    if job and job.status in _TERMINAL_STATUSES:
        return job
    return _update(
        job_id,
        status=BackgroundJobStatus.RUNNING,
        progress=progress,
        started_at=_now(),
    )


def update_progress(job_id: uuid.UUID | str, progress: str) -> None:
    job = get_job(job_id)
    if job and job.status in _TERMINAL_STATUSES:
        return
    _update(job_id, progress=progress)


def raise_if_job_cancelled(job_id: uuid.UUID | str) -> None:
    """Прерывает выполнение, если задача уже отменена или завершена."""
    job = get_job(job_id)
    if not job:
        raise JobCancelledError(JOB_CANCELLED_MESSAGE)
    if job.status in _TERMINAL_STATUSES:
        raise JobCancelledError(job.error or JOB_CANCELLED_MESSAGE)


def finish_job_ok(
    job_id: uuid.UUID | str,
    result: dict[str, Any],
    *,
    progress: str = "Готово",
) -> Optional[BackgroundJob]:
    job = get_job(job_id)
    if job and job.status in _TERMINAL_STATUSES:
        return job
    return _update(
        job_id,
        status=BackgroundJobStatus.DONE,
        progress=progress,
        result=result,
        finished_at=_now(),
        error=None,
    )


def finish_job_error(job_id: uuid.UUID | str, message: str) -> Optional[BackgroundJob]:
    job = get_job(job_id)
    if job and job.status in _TERMINAL_STATUSES:
        return job
    return _update(
        job_id,
        status=BackgroundJobStatus.ERROR,
        error=message,
        progress=f"Ошибка: {message}",
        finished_at=_now(),
    )


def list_cancellable_jobs(limit: int = 50) -> list[BackgroundJob]:
    """Задачи в очереди или в работе — их можно отменить."""
    with Session(engine) as session:
        return list(
            session.exec(
                select(BackgroundJob)
                .where(
                    BackgroundJob.status.in_(  # type: ignore[attr-defined]
                        [BackgroundJobStatus.PENDING, BackgroundJobStatus.RUNNING]
                    )
                )
                .order_by(BackgroundJob.created_at.desc())  # type: ignore[arg-type]
                .limit(limit)
            ).all()
        )


def list_active_jobs(limit: int = 20) -> list[BackgroundJob]:
    """Активные задачи (PENDING/RUNNING) + только что завершившиеся (для уведомления)."""
    with Session(engine) as session:
        active = list(
            session.exec(
                select(BackgroundJob)
                .where(
                    BackgroundJob.status.in_(  # type: ignore[attr-defined]
                        [BackgroundJobStatus.PENDING, BackgroundJobStatus.RUNNING]
                    )
                )
                .order_by(BackgroundJob.created_at.desc())  # type: ignore[arg-type]
                .limit(limit)
            ).all()
        )
        recent_cutoff = _now() - timedelta(minutes=10)
        recent = list(
            session.exec(
                select(BackgroundJob)
                .where(
                    BackgroundJob.status.in_(  # type: ignore[attr-defined]
                        [BackgroundJobStatus.DONE, BackgroundJobStatus.ERROR]
                    )
                )
                .where(BackgroundJob.finished_at >= recent_cutoff)  # type: ignore[operator]
                .order_by(BackgroundJob.finished_at.desc())  # type: ignore[arg-type]
                .limit(limit)
            ).all()
        )
    return active + recent


def list_recent_jobs(limit: int = 50) -> list[BackgroundJob]:
    with Session(engine) as session:
        return list(
            session.exec(
                select(BackgroundJob)
                .order_by(BackgroundJob.created_at.desc())  # type: ignore[arg-type]
                .limit(limit)
            ).all()
        )


def serialize_job(job: BackgroundJob, *, include_result: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": str(job.id),
        "type": job.type.value,
        "status": job.status.value,
        "title": job.title,
        "progress": job.progress,
        "project_id": str(job.project_id) if job.project_id else None,
        "payload": job.payload or {},
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    if include_result:
        data["result"] = job.result
    return data
