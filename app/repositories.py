from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field

from sqlmodel import Session, col, select

from app.models import Employee, InternalProject, ProjectEmployee, ProjectSource, ReportHistory, SyncMeeting, SyncTask
from app.services.report_history import report_display_name
from app.services.sync_meetings import get_tasks_for_meeting, list_meetings_for_report


def get_project_with_sources(session: Session, project_id: uuid.UUID) -> tuple[InternalProject | None, list[ProjectSource]]:
    project = session.get(InternalProject, project_id)
    if not project:
        return None, []
    sources = session.exec(
        select(ProjectSource).where(ProjectSource.project_id == project_id)
    ).all()
    return project, list(sources)


def list_projects_with_sources(
    session: Session,
) -> list[tuple[InternalProject, list[ProjectSource], list[Employee]]]:
    projects = list(
        session.exec(select(InternalProject).order_by(InternalProject.created_at.desc())).all()
    )
    if not projects:
        return []

    project_ids = [p.id for p in projects]
    sources_rows = session.exec(
        select(ProjectSource).where(col(ProjectSource.project_id).in_(project_ids))
    ).all()
    sources_by_project: dict[uuid.UUID, list[ProjectSource]] = defaultdict(list)
    for src in sources_rows:
        sources_by_project[src.project_id].append(src)

    emp_rows = session.exec(
        select(Employee, ProjectEmployee.project_id)
        .join(ProjectEmployee, ProjectEmployee.employee_id == Employee.id)
        .where(col(ProjectEmployee.project_id).in_(project_ids))
        .order_by(Employee.full_name)
    ).all()
    employees_by_project: dict[uuid.UUID, list[Employee]] = defaultdict(list)
    for emp, pid in emp_rows:
        employees_by_project[pid].append(emp)

    return [
        (p, sources_by_project.get(p.id, []), employees_by_project.get(p.id, []))
        for p in projects
    ]


def list_employees(session: Session) -> list[Employee]:
    return list(session.exec(select(Employee).order_by(Employee.full_name)).all())


def get_project_employees(session: Session, project_id: uuid.UUID) -> list[Employee]:
    rows = session.exec(
        select(Employee)
        .join(ProjectEmployee, ProjectEmployee.employee_id == Employee.id)
        .where(ProjectEmployee.project_id == project_id)
        .order_by(Employee.full_name)
    ).all()
    return list(rows)


def get_employee_project_ids(session: Session, employee_id: uuid.UUID) -> list[uuid.UUID]:
    rows = session.exec(
        select(ProjectEmployee.project_id).where(ProjectEmployee.employee_id == employee_id)
    ).all()
    return list(rows)


def _sort_meetings(meetings: list[SyncMeeting]) -> list[SyncMeeting]:
    return sorted(
        meetings,
        key=lambda m: (m.meeting_at or m.created_at, m.created_at),
        reverse=True,
    )


def list_history_report_rows(
    session: Session,
    *,
    limit: int = 80,
) -> list[tuple[ReportHistory, InternalProject | None, list[SyncMeeting], list[SyncTask]]]:
    """Отчёты для страницы истории: несколько встреч на отчёт."""
    reports = list(
        session.exec(
            select(ReportHistory, InternalProject)
            .outerjoin(InternalProject, ReportHistory.project_id == InternalProject.id)
            .order_by(ReportHistory.created_at.desc())
            .limit(limit)
        ).all()
    )
    if not reports:
        return []

    report_ids = [r.id for r, _ in reports]
    meetings = list(
        session.exec(select(SyncMeeting).where(col(SyncMeeting.report_id).in_(report_ids))).all()
    )
    meetings_by_report: dict[uuid.UUID, list[SyncMeeting]] = defaultdict(list)
    for m in meetings:
        if m.report_id:
            meetings_by_report[m.report_id].append(m)
    for rid in meetings_by_report:
        meetings_by_report[rid] = _sort_meetings(meetings_by_report[rid])

    meeting_ids = [m.id for m in meetings]
    tasks_by_meeting: dict[uuid.UUID, list[SyncTask]] = defaultdict(list)
    if meeting_ids:
        tasks = session.exec(
            select(SyncTask)
            .where(col(SyncTask.meeting_id).in_(meeting_ids))
            .order_by(SyncTask.sort_order, SyncTask.created_at)
        ).all()
        for t in tasks:
            tasks_by_meeting[t.meeting_id].append(t)

    result: list[tuple[ReportHistory, InternalProject | None, list[SyncMeeting], list[SyncTask]]] = []
    for report, project in reports:
        mlist = meetings_by_report.get(report.id, [])
        tasks: list[SyncTask] = []
        for m in mlist:
            tasks.extend(tasks_by_meeting.get(m.id, []))
        result.append((report, project, mlist, tasks))
    return result


def get_report_row(
    session: Session,
    report_id: uuid.UUID,
) -> tuple[ReportHistory, InternalProject | None, list[SyncMeeting], list[SyncTask]] | None:
    report = session.get(ReportHistory, report_id)
    if not report:
        return None
    project = session.get(InternalProject, report.project_id) if report.project_id else None
    meetings = list_meetings_for_report(session, report_id)
    tasks: list[SyncTask] = []
    for meeting in meetings:
        tasks.extend(get_tasks_for_meeting(session, meeting.id))
    return report, project, meetings, tasks


@dataclass
class SyncMeetingWithTasks:
    meeting: SyncMeeting
    tasks: list[SyncTask] = field(default_factory=list)


@dataclass
class SyncTaskGroup:
    report_id: uuid.UUID
    report_name: str
    report_date: str
    rag_status: str
    meetings: list[SyncMeetingWithTasks]
    tasks: list[SyncTask]


def list_sync_task_groups(session: Session, *, limit: int = 80) -> list[SyncTaskGroup]:
    """Задачи из расшифровок, сгруппированные по отчётам (внутри — по встречам)."""
    groups: list[SyncTaskGroup] = []
    for report, project, meetings, all_tasks in list_history_report_rows(session, limit=limit):
        if not meetings:
            continue
        meeting_blocks: list[SyncMeetingWithTasks] = []
        tasks_by_meeting: dict[uuid.UUID, list[SyncTask]] = defaultdict(list)
        for t in all_tasks:
            tasks_by_meeting[t.meeting_id].append(t)
        for m in meetings:
            meeting_blocks.append(
                SyncMeetingWithTasks(meeting=m, tasks=tasks_by_meeting.get(m.id, []))
            )
        groups.append(
            SyncTaskGroup(
                report_id=report.id,
                report_name=report_display_name(report, project),
                report_date=report.created_at.strftime("%d.%m.%Y %H:%M"),
                rag_status=report.rag_status.value,
                meetings=meeting_blocks,
                tasks=all_tasks,
            )
        )
    return groups


def delete_report_with_relations(session: Session, report_id: uuid.UUID) -> bool:
    """Удаляет отчёт; встречи и задачи синка удаляются каскадом."""
    report = session.get(ReportHistory, report_id)
    if not report:
        return False
    session.delete(report)
    session.commit()
    return True
