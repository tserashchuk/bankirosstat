from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from app.models import ProjectRoadmap


def get_project_roadmap(session: Session, project_id: uuid.UUID) -> ProjectRoadmap | None:
    return session.exec(
        select(ProjectRoadmap).where(ProjectRoadmap.project_id == project_id)
    ).first()


def save_project_roadmap(
    session: Session,
    project_id: uuid.UUID,
    *,
    columns: list[str],
    rows: list[dict[str, Any]],
    sources_used: list[str],
    warnings: list[str],
) -> ProjectRoadmap:
    existing = get_project_roadmap(session, project_id)
    if existing:
        existing.columns = columns
        existing.rows = rows
        existing.sources_used = sources_used
        existing.warnings = warnings
        existing.saved_at = datetime.utcnow()
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing

    record = ProjectRoadmap(
        project_id=project_id,
        columns=columns,
        rows=rows,
        sources_used=sources_used,
        warnings=warnings,
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record


def roadmap_to_api_payload(roadmap: ProjectRoadmap) -> dict[str, Any]:
    return {
        "columns": roadmap.columns or [],
        "rows": roadmap.rows or [],
        "meta": {
            "from_db": True,
            "saved_at": roadmap.saved_at.isoformat() if roadmap.saved_at else None,
            "sources_used": roadmap.sources_used or [],
            "warnings": roadmap.warnings or [],
            "rows_returned": len(roadmap.rows or []),
        },
        "saved": True,
        "saved_at": roadmap.saved_at.isoformat() if roadmap.saved_at else None,
    }
