"""Milestones проекта: CRUD, даты и попадание в snapshot для LLM."""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any

from sqlmodel import Session, col, select

from app.models import ProjectMilestone

_DATE_DMY = re.compile(
    r"^(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2}|\d{4})$"
)
_DATE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def parse_deadline(raw: str | None) -> str | None:
    """Нормализует deadline в ISO YYYY-MM-DD. Пустое → None."""
    text = (raw or "").strip()
    if not text:
        return None
    m = _DATE_ISO.match(text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        date(y, mo, d)
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = _DATE_DMY.match(text)
    if not m:
        raise ValueError(f"Некорректная дата: {text}")
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000 if y < 70 else 1900
    date(y, mo, d)
    return f"{y:04d}-{mo:02d}-{d:02d}"


def deadline_label(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = date.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.strftime("%d.%m.%Y")


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def days_until_deadline(iso: str | None, *, today: date | None = None) -> int | None:
    if not iso:
        return None
    try:
        due = date.fromisoformat(iso)
    except ValueError:
        return None
    return (due - (today or _today_utc())).days


def serialize_milestone(
    row: ProjectMilestone, *, today: date | None = None
) -> dict[str, Any]:
    due = row.deadline
    until = days_until_deadline(due, today=today)
    return {
        "id": str(row.id),
        "code": row.code,
        "title": row.title,
        "description": row.description or "",
        "deadline": due or "",
        "deadline_label": deadline_label(due),
        "days_until_deadline": until,
        "overdue": until is not None and until < 0,
        "sort_order": row.sort_order,
    }


def list_milestones(session: Session, project_id: uuid.UUID) -> list[ProjectMilestone]:
    return list(
        session.exec(
            select(ProjectMilestone)
            .where(ProjectMilestone.project_id == project_id)
            .order_by(ProjectMilestone.sort_order, ProjectMilestone.created_at)
        ).all()
    )


def list_milestones_by_project_ids(
    session: Session, project_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[ProjectMilestone]]:
    out: dict[uuid.UUID, list[ProjectMilestone]] = defaultdict(list)
    if not project_ids:
        return out
    rows = session.exec(
        select(ProjectMilestone)
        .where(col(ProjectMilestone.project_id).in_(project_ids))
        .order_by(ProjectMilestone.sort_order, ProjectMilestone.created_at)
    ).all()
    for row in rows:
        out[row.project_id].append(row)
    return out


def _auto_code(index: int, existing: set[str]) -> str:
    n = index + 1
    while True:
        code = f"M{n}"
        if code not in existing:
            return code
        n += 1


def replace_milestones(
    session: Session,
    project_id: uuid.UUID,
    items: list[dict[str, Any]],
) -> list[ProjectMilestone]:
    """Заменяет набор milestones проекта. Пустые строки (без названия) пропускаются."""
    cleaned: list[dict[str, Any]] = []
    for raw in items:
        title = str(raw.get("title") or "").strip()
        code = str(raw.get("code") or "").strip()
        description = str(raw.get("description") or "").strip()
        if not title and not code and not description:
            continue
        if not title:
            raise ValueError("Укажите название milestone")
        try:
            deadline = parse_deadline(str(raw.get("deadline") or "") or None)
        except ValueError as exc:
            raise ValueError(f"{code or title}: {exc}") from exc
        mid = raw.get("id") or None
        if isinstance(mid, str) and mid.strip():
            try:
                mid = uuid.UUID(mid.strip())
            except ValueError as exc:
                raise ValueError(f"Некорректный id milestone: {mid}") from exc
        elif not mid:
            mid = None
        cleaned.append(
            {
                "id": mid,
                "code": code,
                "title": title[:500],
                "description": description or None,
                "deadline": deadline,
            }
        )

    existing = {
        row.id: row for row in list_milestones(session, project_id)
    }
    keep_ids: set[uuid.UUID] = set()
    used_codes: set[str] = set()
    result: list[ProjectMilestone] = []
    now = datetime.utcnow()

    for idx, item in enumerate(cleaned):
        code = item["code"] or _auto_code(idx, used_codes)
        used_codes.add(code)
        row_id = item["id"]
        row = existing.get(row_id) if row_id else None
        if row is None:
            row = ProjectMilestone(project_id=project_id)
        row.code = code[:32]
        row.title = item["title"]
        row.description = item["description"]
        row.deadline = item["deadline"]
        row.sort_order = idx
        row.updated_at = now
        session.add(row)
        result.append(row)
        if row.id:
            keep_ids.add(row.id)

    for old_id, old in existing.items():
        if old_id not in keep_ids:
            session.delete(old)

    session.commit()
    for row in result:
        session.refresh(row)
    return list_milestones(session, project_id)


def delete_project_milestones(session: Session, project_id: uuid.UUID) -> None:
    for row in list_milestones(session, project_id):
        session.delete(row)


def enrich_snapshot_with_milestones(
    snapshot: dict[str, Any],
    session: Session,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    rows = list_milestones(session, project_id)
    today = _today_utc()
    snapshot["milestones"] = [serialize_milestone(r, today=today) for r in rows]
    snapshot["milestones_meta"] = {
        "count": len(rows),
        "as_of": today.isoformat(),
        "from_db": bool(rows),
    }
    return snapshot
