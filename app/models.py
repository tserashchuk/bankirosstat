from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, JSON, Text
from sqlmodel import Field, SQLModel


class SourceType(str, enum.Enum):
    YOUTRACK = "youtrack"
    EMAIL = "email"
    CALENDAR = "calendar"
    GOOGLE_DOC = "google_doc"
    GOOGLE_SHEET = "google_sheet"


class RagStatus(str, enum.Enum):
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"


class TaskPriority(str, enum.Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TaskStatus(str, enum.Enum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class InternalProject(SQLModel, table=True):
    __tablename__ = "internal_projects"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(max_length=255)
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProjectRoadmap(SQLModel, table=True):
    """Сохранённый roadmap проекта (все колонки и строки листа)."""

    __tablename__ = "project_roadmaps"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(
        foreign_key="internal_projects.id",
        unique=True,
        ondelete="CASCADE",
    )
    columns: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    rows: list[dict] = Field(default_factory=list, sa_column=Column(JSON))
    sources_used: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    warnings: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    saved_at: datetime = Field(default_factory=datetime.utcnow)


class ProjectSource(SQLModel, table=True):
    __tablename__ = "project_sources"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="internal_projects.id", ondelete="CASCADE")
    source_type: SourceType
    label: Optional[str] = Field(default=None, max_length=255)
    credentials_encrypted: str = Field(sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Employee(SQLModel, table=True):
    __tablename__ = "employees"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    full_name: str = Field(max_length=255)
    yandex_email: str = Field(max_length=255, index=True)
    imap_password_encrypted: Optional[str] = Field(default=None, sa_column=Column(Text))
    calendar_password_encrypted: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProjectEmployee(SQLModel, table=True):
    __tablename__ = "project_employees"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="internal_projects.id", ondelete="CASCADE")
    employee_id: uuid.UUID = Field(foreign_key="employees.id", ondelete="CASCADE")
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ReportHistory(SQLModel, table=True):
    __tablename__ = "reports_history"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: Optional[uuid.UUID] = Field(
        default=None,
        foreign_key="internal_projects.id",
    )
    title: Optional[str] = Field(default=None, max_length=255)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    model_used: str = Field(max_length=128)
    raw_data_snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON))
    generated_text: str = Field(sa_column=Column(Text))
    rag_status: RagStatus = Field(default=RagStatus.AMBER)
    gamma_generation_id: Optional[str] = Field(default=None, max_length=128)
    gamma_url: Optional[str] = Field(default=None, sa_column=Column(Text))
    gamma_export_url: Optional[str] = Field(default=None, sa_column=Column(Text))


class SyncMeeting(SQLModel, table=True):
    """Расшифровка созвона/синка — только в БД этой системы."""

    __tablename__ = "sync_meetings"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    report_id: Optional[uuid.UUID] = Field(
        default=None,
        foreign_key="reports_history.id",
        ondelete="CASCADE",
        index=True,
    )
    project_id: uuid.UUID = Field(foreign_key="internal_projects.id", ondelete="CASCADE")
    title: str = Field(max_length=255)
    transcript: str = Field(sa_column=Column(Text))
    meeting_at: Optional[datetime] = Field(default=None)
    model_used: Optional[str] = Field(default=None, max_length=128)
    extracted_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class SyncTask(SQLModel, table=True):
    """Задачи из расшифровки — только в БД, без отправки в YouTrack и др."""

    __tablename__ = "sync_tasks"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    meeting_id: uuid.UUID = Field(foreign_key="sync_meetings.id", ondelete="CASCADE")
    project_id: uuid.UUID = Field(foreign_key="internal_projects.id", ondelete="CASCADE")
    title: str = Field(max_length=500)
    description: Optional[str] = Field(default=None, sa_column=Column(Text))
    assignee: Optional[str] = Field(default=None, max_length=255)
    due_date: Optional[str] = Field(default=None, max_length=32)
    priority: TaskPriority = Field(default=TaskPriority.MEDIUM)
    status: TaskStatus = Field(default=TaskStatus.OPEN)
    sort_order: int = Field(default=0)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class BackgroundJobType(str, enum.Enum):
    REPORT_GENERATE = "report.generate"
    ROADMAP_SAVE = "roadmap.save"
    SYNC_ATTACH = "sync.attach"
    SYNC_REEXTRACT = "sync.reextract"


class BackgroundJobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


class BackgroundJob(SQLModel, table=True):
    """Фоновая задача — запускается в worker'е (ARQ), статус и результат хранятся в БД."""

    __tablename__ = "background_jobs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    type: BackgroundJobType
    status: BackgroundJobStatus = Field(default=BackgroundJobStatus.PENDING, index=True)
    title: str = Field(default="", max_length=255)
    progress: str = Field(default="Ожидание...", max_length=512)
    project_id: Optional[uuid.UUID] = Field(default=None, index=True)
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    result: Optional[dict] = Field(default=None, sa_column=Column(JSON))
    error: Optional[str] = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    started_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)
