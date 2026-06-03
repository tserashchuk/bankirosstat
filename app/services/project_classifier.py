from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.models import Employee, InternalProject, ProjectSource, SourceType

_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]{3,}", re.UNICODE)
_ISSUE_ID_RE = re.compile(r"\b[A-Z][A-Z0-9_]{1,15}-\d+\b")


def _empty_snapshot() -> dict[str, Any]:
    return {
        "youtrack": [],
        "email": [],
        "employee_emails": [],
        "calendar": [],
        "google_doc": None,
        "google_sheets": [],
        "roadmap_table": [],
        "roadmap_meta": {},
        "routing": {},
    }


def _tokenize(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


def _extract_description_context(description: str | None) -> tuple[list[str], set[str]]:
    """Фразы и ключевые слова из описания проекта для сегментации."""
    if not description or not description.strip():
        return [], set()
    phrases: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[\n;|,]+", description):
        p = " ".join(part.split()).strip()
        if len(p) < 4:
            continue
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        phrases.append(key)

    keywords: set[str] = set()
    for phrase in phrases:
        keywords.update(_tokenize(phrase))
    keywords.update(_tokenize(description))
    return phrases, {k for k in keywords if len(k) >= 3}


@dataclass
class ProjectProfile:
    project_id: uuid.UUID
    name: str
    description: str | None
    keywords: set[str] = field(default_factory=set)
    description_phrases: list[str] = field(default_factory=list)
    description_keywords: set[str] = field(default_factory=set)
    youtrack_ids: set[str] = field(default_factory=set)
    employee_names: list[str] = field(default_factory=list)
    employee_emails: list[str] = field(default_factory=list)
    source_labels: list[str] = field(default_factory=list)


@dataclass
class SharedPool:
    messages: list[dict[str, Any]] = field(default_factory=list)
    calendar_events: list[dict[str, Any]] = field(default_factory=list)


def build_project_profiles(
    targets: list[tuple[InternalProject, list[ProjectSource], list[Employee]]],
) -> list[ProjectProfile]:
    profiles: list[ProjectProfile] = []

    for project, sources, employees in targets:
        keywords: set[str] = set()
        keywords.update(_tokenize(project.name))
        desc_phrases, desc_kw = _extract_description_context(project.description)
        keywords.update(desc_kw)

        youtrack_ids: set[str] = set()
        labels: list[str] = []

        for src in sources:
            if src.label:
                labels.append(src.label)
                keywords.update(_tokenize(src.label))
            try:
                from app.services.source_creds import decrypt_normalized

                creds = decrypt_normalized(src.credentials_encrypted)
            except Exception:
                creds = {}
            if src.source_type == SourceType.YOUTRACK:
                pid = creds.get("project_id")
                if pid:
                    youtrack_ids.add(str(pid).upper())
                    keywords.add(str(pid).lower())
                comp = creds.get("component_name")
                if comp:
                    keywords.update(_tokenize(str(comp)))
            elif src.source_type == SourceType.GOOGLE_DOC:
                doc = creds.get("document_id", "")
                if doc:
                    keywords.add(doc[:12].lower())
            elif src.source_type == SourceType.GOOGLE_SHEET:
                sheet = creds.get("sheet_name") or creds.get("spreadsheet_id", "")
                keywords.update(_tokenize(str(sheet)))

        emp_names = [e.full_name for e in employees if e.full_name]
        emp_emails = [e.yandex_email for e in employees if e.yandex_email]
        for n in emp_names:
            keywords.update(_tokenize(n))
        for em in emp_emails:
            keywords.add(em.lower())
            local = em.split("@")[0]
            if len(local) >= 3:
                keywords.add(local.lower())

        profiles.append(
            ProjectProfile(
                project_id=project.id,
                name=project.name,
                description=project.description,
                keywords={k for k in keywords if len(k) >= 3},
                description_phrases=desc_phrases,
                description_keywords=desc_kw,
                youtrack_ids=youtrack_ids,
                employee_names=emp_names,
                employee_emails=emp_emails,
                source_labels=labels,
            )
        )

    return profiles


def _text_blob(item: dict[str, Any]) -> str:
    parts = [
        item.get("subject", ""),
        item.get("snippet", ""),
        item.get("from", ""),
        item.get("summary", ""),
        item.get("from_email", ""),
    ]
    return " ".join(str(p) for p in parts if p).lower()


def score_for_project(item: dict[str, Any], profile: ProjectProfile) -> float:
    raw = _text_blob(item)
    if not raw.strip():
        return 0.0

    text = raw.lower()
    text_upper = raw.upper()
    score = 0.0
    name_lower = profile.name.lower()

    if name_lower and name_lower in text:
        score += 8.0

    for phrase in profile.description_phrases:
        if phrase in text:
            score += 14.0

    for kw in profile.description_keywords:
        if kw in text:
            score += 4.0

    for kw in profile.keywords - profile.description_keywords:
        if kw in text:
            score += 2.5

    for yt_id in profile.youtrack_ids:
        if yt_id in text_upper:
            score += 6.0
    for match in _ISSUE_ID_RE.findall(text_upper):
        prefix = match.split("-")[0]
        if prefix in profile.youtrack_ids:
            score += 7.0

    for em in profile.employee_emails:
        if em and em.lower() in text:
            score += 3.0
    for nm in profile.employee_names:
        parts = nm.lower().split()
        if parts and parts[0] in text:
            score += 2.0

    hint = item.get("_hint_project_id")
    if hint and str(hint) == str(profile.project_id):
        score += 4.0

    return score


def _employee_single_project_map(
    profiles: list[ProjectProfile],
) -> dict[str, uuid.UUID]:
    email_to_projects: dict[str, list[uuid.UUID]] = {}
    for p in profiles:
        for em in p.employee_emails:
            email_to_projects.setdefault(em.lower(), []).append(p.project_id)

    result: dict[str, uuid.UUID] = {}
    for em, pids in email_to_projects.items():
        if len(pids) == 1:
            result[em] = pids[0]
    return result


def assign_to_project(
    item: dict[str, Any],
    profiles: list[ProjectProfile],
    employee_only: dict[str, uuid.UUID],
) -> tuple[uuid.UUID | None, float, str]:
    """Возвращает (project_id, score, reason)."""
    emp_mail = (item.get("_employee_email") or item.get("mailbox") or "").lower()
    if item.get("_origin") in ("employee_calendar", "employee_invite") and emp_mail in employee_only:
        return employee_only[emp_mail], 3.0, "employee_calendar"

    scores = [(p.project_id, score_for_project(item, p)) for p in profiles]
    best_id, best_score = max(scores, key=lambda x: x[1])

    if best_score < 2.0 and emp_mail in employee_only:
        return employee_only[emp_mail], 1.5, "employee_single_project"

    if best_score < 2.0:
        return None, best_score, "low_confidence"

    return best_id, best_score, "content_match"


def route_pool_to_snapshots(
    profiles: list[ProjectProfile],
    pool: SharedPool,
    bound: dict[uuid.UUID, dict[str, Any]],
) -> dict[uuid.UUID, dict[str, Any]]:
    """Распределяет общие письма/встречи по проектам и мержит с привязанными источниками."""
    employee_only = _employee_single_project_map(profiles)
    snapshots: dict[uuid.UUID, dict[str, Any]] = {}

    for p in profiles:
        snap = _empty_snapshot()
        base = bound.get(p.project_id, _empty_snapshot())
        snap["youtrack"] = list(base.get("youtrack", []))
        snap["google_doc"] = base.get("google_doc")
        snap["google_sheets"] = list(base.get("google_sheets", []))
        snap["email"] = list(base.get("email", []))
        snap["calendar"] = list(base.get("calendar", []))
        snapshots[p.project_id] = snap

    routing_stats: dict[str, dict[str, int]] = {
        str(p.project_id): {"messages": 0, "events": 0} for p in profiles
    }
    unassigned_messages: list[dict[str, Any]] = []
    unassigned_events: list[dict[str, Any]] = []

    mail_by_project: dict[uuid.UUID, list[dict[str, str]]] = {p.project_id: [] for p in profiles}
    events_by_project: dict[uuid.UUID, list[dict[str, Any]]] = {p.project_id: [] for p in profiles}

    for msg in pool.messages:
        pid, sc, _reason = assign_to_project(msg, profiles, employee_only)
        clean = {k: v for k, v in msg.items() if not k.startswith("_")}
        clean["routing_score"] = round(sc, 1)
        if pid:
            mail_by_project[pid].append(clean)
            routing_stats[str(pid)]["messages"] += 1
        else:
            unassigned_messages.append(clean)

    for ev in pool.calendar_events:
        pid, sc, _reason = assign_to_project(ev, profiles, employee_only)
        clean = {k: v for k, v in ev.items() if not k.startswith("_")}
        clean["routing_score"] = round(sc, 1)
        if pid:
            events_by_project[pid].append(clean)
            routing_stats[str(pid)]["events"] += 1
        else:
            unassigned_events.append(clean)

    for p in profiles:
        snap = snapshots[p.project_id]
        pid = p.project_id

        extra_mail = mail_by_project.get(pid, [])
        if extra_mail:
            snap["employee_emails"].append(
                {
                    "source": "routed_mail",
                    "employee": "Портфель (автораспределение)",
                    "mailbox": "",
                    "messages": extra_mail,
                    "meetings": [],
                }
            )

        extra_events = events_by_project.get(pid, [])
        if extra_events:
            snap["calendar"].append(
                {
                    "source": "routed_calendar",
                    "label": "Встречи (автораспределение)",
                    "events": extra_events,
                }
            )

        snap["routing"] = {
            "project_id": str(pid),
            "project_name": p.name,
            "project_description": p.description or "",
            "context_phrases": p.description_phrases[:15],
            "assigned_messages": routing_stats[str(pid)]["messages"],
            "assigned_events": routing_stats[str(pid)]["events"],
            "portfolio_unassigned_messages": len(unassigned_messages),
            "portfolio_unassigned_events": len(unassigned_events),
        }

    if unassigned_messages or unassigned_events:
        first_pid = profiles[0].project_id
        snap = snapshots[first_pid]
        snap["routing"]["unassigned_note"] = (
            "Часть данных не сопоставлена однозначно — см. unassigned_* в портфеле"
        )
        snap.setdefault("_portfolio_unassigned", {})
        snap["_portfolio_unassigned"] = {
            "messages": unassigned_messages[:20],
            "events": unassigned_events[:20],
        }

    return snapshots


def merge_email_meetings(snapshot: dict[str, Any]) -> None:
    for block in snapshot.get("email", []):
        if block.get("skipped"):
            continue
        meetings = block.pop("meetings", None)
        if meetings:
            snapshot["calendar"].append(
                {
                    "source": "email_invites",
                    "mailbox": block.get("mailbox"),
                    "label": block.get("label"),
                    "events": meetings,
                }
            )
    for block in snapshot.get("employee_emails", []):
        if block.get("skipped"):
            continue
        meetings = block.pop("meetings", None)
        if meetings:
            snapshot["calendar"].append(
                {
                    "source": "employee_email_invites",
                    "employee": block.get("employee"),
                    "mailbox": block.get("mailbox"),
                    "events": meetings,
                }
            )
