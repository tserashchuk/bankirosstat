from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

settings = get_settings()

connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args["check_same_thread"] = False
    db_path = settings.database_url.replace("sqlite:///", "")
    if db_path.startswith("./"):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

engine = create_engine(settings.database_url, connect_args=connect_args)


def _migrate_sqlite_columns() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(employees)")).fetchall()
        columns = {row[1] for row in rows}
        if "calendar_password_encrypted" not in columns:
            conn.execute(
                text("ALTER TABLE employees ADD COLUMN calendar_password_encrypted TEXT")
            )
            conn.commit()

        meeting_rows = conn.execute(text("PRAGMA table_info(sync_meetings)")).fetchall()
        meeting_cols = {row[1] for row in meeting_rows}
        if meeting_rows and "report_id" not in meeting_cols:
            conn.execute(text("ALTER TABLE sync_meetings ADD COLUMN report_id TEXT"))
            conn.commit()

        report_rows = conn.execute(text("PRAGMA table_info(reports_history)")).fetchall()
        report_cols = {row[1] for row in report_rows}
        if report_rows and "title" not in report_cols:
            conn.execute(text("ALTER TABLE reports_history ADD COLUMN title TEXT"))
            conn.commit()
        if report_rows and "gamma_generation_id" not in report_cols:
            conn.execute(text("ALTER TABLE reports_history ADD COLUMN gamma_generation_id TEXT"))
            conn.commit()
        if report_rows and "gamma_url" not in report_cols:
            conn.execute(text("ALTER TABLE reports_history ADD COLUMN gamma_url TEXT"))
            conn.commit()
        if report_rows and "gamma_export_url" not in report_cols:
            conn.execute(text("ALTER TABLE reports_history ADD COLUMN gamma_export_url TEXT"))
            conn.commit()

        _migrate_sync_meetings_multi_per_report(conn)


def _migrate_sync_meetings_multi_per_report(conn) -> None:
    """Убирает UNIQUE(report_id), чтобы к одному отчёту можно было привязать несколько встреч."""
    meeting_rows = conn.execute(text("PRAGMA table_info(sync_meetings)")).fetchall()
    if not meeting_rows:
        return

    unique_on_report = False
    for idx in conn.execute(text("PRAGMA index_list(sync_meetings)")).fetchall():
        if not idx[2]:
            continue
        idx_name = idx[1]
        cols = conn.execute(text(f'PRAGMA index_info("{idx_name}")')).fetchall()
        if [c[2] for c in cols] == ["report_id"]:
            unique_on_report = True
            break

    if not unique_on_report:
        return

    conn.execute(text("PRAGMA foreign_keys=OFF"))
    conn.execute(
        text(
            """
            CREATE TABLE sync_meetings_new (
                id TEXT NOT NULL PRIMARY KEY,
                report_id TEXT,
                project_id TEXT NOT NULL,
                title VARCHAR(255) NOT NULL,
                transcript TEXT NOT NULL,
                meeting_at TEXT,
                model_used VARCHAR(128),
                extracted_at TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(report_id) REFERENCES reports_history(id) ON DELETE CASCADE,
                FOREIGN KEY(project_id) REFERENCES internal_projects(id) ON DELETE CASCADE
            )
            """
        )
    )
    conn.execute(
        text(
            """
            INSERT INTO sync_meetings_new
            SELECT id, report_id, project_id, title, transcript, meeting_at,
                   model_used, extracted_at, created_at
            FROM sync_meetings
            """
        )
    )
    conn.execute(text("DROP TABLE sync_meetings"))
    conn.execute(text("ALTER TABLE sync_meetings_new RENAME TO sync_meetings"))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sync_meetings_report_id ON sync_meetings (report_id)"))
    conn.execute(text("PRAGMA foreign_keys=ON"))
    conn.commit()


def _sqlite_pragmas() -> None:
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA synchronous=NORMAL"))
        conn.commit()


def init_db() -> None:
    import app.models  # noqa: F401 — register tables in metadata

    SQLModel.metadata.create_all(engine)
    _sqlite_pragmas()
    _migrate_sqlite_columns()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
