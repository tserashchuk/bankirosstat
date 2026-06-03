from __future__ import annotations

from app.models import InternalProject, ReportHistory


def report_display_name(report: ReportHistory, project: InternalProject | None) -> str:
    """Название строки в истории: title, портфель или проект."""
    if report.title and report.title.strip():
        return report.title.strip()
    snap = report.raw_data_snapshot or {}
    if snap.get("portfolio"):
        projects = snap.get("projects") or []
        n = len(projects)
        return f"Портфель ({n} проектов)" if n else "Портфель"
    if project:
        return project.name
    return "Отчёт"
