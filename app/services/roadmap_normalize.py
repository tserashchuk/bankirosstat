from __future__ import annotations

import csv
import io
import re
import uuid
from typing import Any

from sqlmodel import Session

from app.models import ProjectSource, SourceType
from app.services.collectors.google_doc import collect_google_doc
from app.services.collectors.google_sheet import collect_google_sheet
from app.services.llm.prompts import current_quarter_label
from app.services.roadmap_repository import get_project_roadmap

# Заголовок колонки (нижний регистр) → поле внутренней таблицы
_COLUMN_ALIASES: dict[str, str] = {
    "эпик": "initiative",
    "epic": "initiative",
    "инициатива": "initiative",
    "инициативы": "initiative",
    "название": "initiative",
    "name": "initiative",
    "title": "initiative",
    "задача": "initiative",
    "feature": "initiative",
    "проект": "initiative",
    "статус": "status",
    "status": "status",
    "state": "status",
    "этап": "status",
    "квартал": "quarter",
    "quarter": "quarter",
    "q": "quarter",
    "срок": "due",
    "due": "due",
    "deadline": "due",
    "дата": "due",
    "веха": "due",
    "milestone": "due",
    "ответственный": "owner",
    "owner": "owner",
    "assignee": "owner",
    "владелец": "owner",
    "лид": "owner",
    "комментарий": "comment",
    "comment": "comment",
    "notes": "comment",
    "примечание": "comment",
    "описание": "comment",
}

_ROADMAP_ROW_KEYS = ("initiative", "status", "quarter", "due", "owner", "comment")


def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", " ", (cell or "").strip().lower())


def _map_header_name(raw: str) -> str | None:
    key = _norm_header(raw)
    if not key:
        return None
    if key in _COLUMN_ALIASES:
        return _COLUMN_ALIASES[key]
    for alias, field_name in _COLUMN_ALIASES.items():
        if alias in key or key in alias:
            return field_name
    return None


def _map_headers(headers: list[str]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for idx, raw in enumerate(headers):
        field = _map_header_name(raw)
        if field:
            mapping[idx] = field
    return mapping


def _detect_delimiter(sample: str) -> str:
    if sample.count(";") > sample.count(","):
        return ";"
    if sample.count("\t") > sample.count(","):
        return "\t"
    return ","


def _clean_header(cell: str) -> str:
    return (cell or "").strip() or "Колонка"


def _row_has_data(row: dict[str, str]) -> bool:
    return any((v or "").strip() for v in row.values())


def parse_csv_full_rows(csv_text: str, source_label: str) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """Все колонки листа и все строки данных (без фильтра квартала)."""
    warnings: list[str] = []
    text = (csv_text or "").strip()
    if not text:
        return [], [], warnings

    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")

    delimiter = _detect_delimiter(text[:2048])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows_raw = [r for r in reader if any((c or "").strip() for c in r)]
    if not rows_raw:
        return [], [], warnings

    headers = [_clean_header(h) for h in rows_raw[0]]
    # Уникальные заголовки при дубликатах
    seen: dict[str, int] = {}
    unique_headers: list[str] = []
    for h in headers:
        base = h
        if base not in seen:
            seen[base] = 0
            unique_headers.append(base)
        else:
            seen[base] += 1
            unique_headers.append(f"{base} ({seen[base]})")

    out: list[dict[str, str]] = []
    for row in rows_raw[1:]:
        item: dict[str, str] = {}
        for idx, col_name in enumerate(unique_headers):
            val = (row[idx] if idx < len(row) else "").strip()
            item[col_name] = val[:1000]
        if _row_has_data(item):
            out.append(item)

    if not out:
        warnings.append(f"{source_label}: нет строк данных после заголовка")
    return unique_headers, out, warnings


def _parse_doc_lines_full(doc_text: str, source_label: str) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """Doc как одна колонка «Инициатива» + опционально «Статус»."""
    warnings: list[str] = []
    columns = ["Инициатива", "Статус", "Квартал"]
    out: list[dict[str, str]] = []
    text = (doc_text or "").strip()
    if not text:
        return [], [], warnings

    for line in text.splitlines():
        raw = line.strip()
        if not raw or len(raw) < 3:
            continue
        raw = re.sub(r"^[\-\*•\d]+[\.\)]\s*", "", raw).strip()
        if not raw:
            continue

        initiative = raw
        status = quarter = ""
        if " — " in raw:
            parts = [p.strip() for p in raw.split(" — ")]
            initiative = parts[0]
            if len(parts) > 1:
                status = parts[1]
        elif " - " in raw:
            parts = [p.strip() for p in raw.split(" - ")]
            initiative = parts[0]
            if len(parts) > 1:
                status = parts[1]
        elif "|" in raw:
            cells = [c.strip() for c in raw.split("|") if c.strip()]
            if cells:
                initiative = cells[0]
            if len(cells) > 1:
                status = cells[1]
            if len(cells) > 2:
                quarter = cells[2]

        if len(initiative) < 2:
            continue
        out.append(
            {
                columns[0]: initiative[:500],
                columns[1]: status[:200],
                columns[2]: quarter[:50],
            }
        )

    if not out:
        warnings.append(f"{source_label}: из Doc не извлечено строк")
    return columns, out, warnings


def _quarter_matches(value: str, quarter_label: str) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return True
    q = quarter_label.lower()
    q_num = re.search(r"q([1-4])", q)
    year = re.search(r"(20\d{2})", q)
    if q_num and q_num.group(1) in v.replace(" ", ""):
        return True
    if year and year.group(1) in v:
        return True
    if q in v or v in q:
        return True
    return False


def find_quarter_column(columns: list[str]) -> str | None:
    for col in columns:
        if _map_header_name(col) == "quarter":
            return col
    return None


def _canonical_from_row(row: dict[str, str], columns: list[str]) -> dict[str, str]:
    item: dict[str, str] = {k: "" for k in _ROADMAP_ROW_KEYS}
    for col in columns:
        field = _map_header_name(col)
        if field and field in item:
            item[field] = (row.get(col) or "").strip()[:500]
    if not item["initiative"] and columns:
        first = columns[0]
        item["initiative"] = (row.get(first) or "").strip()[:500]
    return item


def filter_rows_for_quarter(
    rows: list[dict[str, str]],
    columns: list[str],
    quarter_label: str,
) -> tuple[list[dict[str, str]], bool]:
    """Строки текущего квартала; если колонки квартала нет — все строки."""
    qcol = find_quarter_column(columns)
    if not qcol:
        return rows, False
    has_any = any((r.get(qcol) or "").strip() for r in rows)
    if not has_any:
        return rows, False
    filtered = [r for r in rows if _quarter_matches(r.get(qcol, ""), quarter_label)]
    if filtered:
        return filtered, True
    return rows, False


def rows_to_llm_table(
    columns: list[str],
    rows: list[dict[str, str]],
    *,
    quarter_filter: bool = True,
    max_rows: int = 80,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    quarter = current_quarter_label()
    working = list(rows)
    quarter_filter_applied = False
    if quarter_filter and working:
        working, quarter_filter_applied = filter_rows_for_quarter(working, columns, quarter)

    llm_rows: list[dict[str, Any]] = []
    for row in working:
        canon = _canonical_from_row(row, columns)
        if not (canon.get("initiative") or "").strip():
            continue
        llm_rows.append({**canon, "cells": {k: row.get(k, "") for k in columns}})

    truncated = 0
    if max_rows and len(llm_rows) > max_rows:
        truncated = len(llm_rows) - max_rows
        llm_rows = llm_rows[:max_rows]

    meta = {
        "quarter": quarter,
        "quarter_filter_applied": quarter_filter_applied,
        "rows_returned": len(llm_rows),
        "rows_in_source": len(rows),
        "llm_rows_truncated": truncated,
    }
    return llm_rows, meta


def import_roadmap_from_google(
    google_doc: Any,
    google_sheets: list[dict[str, Any]],
    *,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """Импорт из Google: все колонки и все строки (без фильтра квартала)."""
    columns: list[str] = []
    all_rows: list[dict[str, str]] = []
    warnings: list[str] = []
    sources_used: list[str] = []

    for sheet in google_sheets or []:
        if not isinstance(sheet, dict) or sheet.get("skipped"):
            continue
        label = sheet.get("sheet_name") or sheet.get("label") or sheet.get("gid") or "Google Sheet"
        cols, rows, w = parse_csv_full_rows(sheet.get("sheet_csv") or "", str(label))
        warnings.extend(w)
        if not rows:
            continue
        sources_used.append(str(label))
        if not columns:
            columns = cols
            all_rows.extend(rows)
        elif cols == columns:
            all_rows.extend(rows)
        else:
            warnings.append(f"{label}: другие заголовки — строки пропущены")
            if not all_rows:
                columns = cols
                all_rows.extend(rows)

    if not all_rows:
        docs = google_doc if isinstance(google_doc, list) else ([google_doc] if google_doc else [])
        for doc in docs:
            if not isinstance(doc, dict) or doc.get("skipped"):
                continue
            label = doc.get("label") or doc.get("document_id") or "Google Doc"
            cols, rows, w = _parse_doc_lines_full(doc.get("roadmap_text") or "", str(label))
            warnings.extend(w)
            if rows:
                sources_used.append(str(label))
                columns = cols
                all_rows.extend(rows)
                break

    if max_rows and len(all_rows) > max_rows:
        warnings.append(f"Показано первых {max_rows} из {len(all_rows)} строк")
        all_rows = all_rows[:max_rows]

    return {
        "columns": columns,
        "rows": all_rows,
        "meta": {
            "quarter": current_quarter_label(),
            "quarter_filter_applied": False,
            "rows_returned": len(all_rows),
            "sources_used": sources_used,
            "warnings": warnings,
        },
    }


def enrich_snapshot_with_roadmap_table(
    snapshot: dict[str, Any],
    session: Session,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    """Roadmap для LLM — только из БД, строки текущего квартала."""
    quarter = current_quarter_label()
    roadmap = get_project_roadmap(session, project_id)
    if not roadmap or not roadmap.rows:
        snapshot["roadmap_table"] = []
        snapshot["roadmap_meta"] = {
            "from_db": False,
            "quarter": quarter,
            "warnings": ["Roadmap не сохранён в базе — сохраните таблицу на странице проектов"],
            "rows_returned": 0,
        }
        return snapshot

    llm_rows, llm_meta = rows_to_llm_table(
        roadmap.columns or [],
        roadmap.rows or [],
        quarter_filter=True,
        max_rows=80,
    )
    snapshot["roadmap_table"] = llm_rows
    snapshot["roadmap_meta"] = {
        "from_db": True,
        "saved_at": roadmap.saved_at.isoformat() if roadmap.saved_at else None,
        "columns": roadmap.columns,
        "quarter": quarter,
        "total_rows_in_db": len(roadmap.rows or []),
        "sources_used": roadmap.sources_used or [],
        **llm_meta,
    }
    return snapshot


async def collect_roadmap_bound(sources: list[ProjectSource]) -> dict[str, Any]:
    """Скачивает только Google Doc/Sheet для превью или сохранения."""
    from app.services.aggregator import _run_collector

    google_doc = None
    google_sheets: list[dict[str, Any]] = []

    tasks: list[Any] = []
    for src in sources:
        if src.source_type == SourceType.GOOGLE_DOC:
            tasks.append(("doc", src))
        elif src.source_type == SourceType.GOOGLE_SHEET:
            tasks.append(("sheet", src))

    async def _one(kind: str, src: ProjectSource):
        fn = collect_google_doc if kind == "doc" else collect_google_sheet
        data = await _run_collector(src, fn, is_async=True)
        if data.get("skipped"):
            return None
        label = src.label or kind
        data["label"] = label
        return kind, data

    if tasks:
        import asyncio

        results = await asyncio.gather(*[_one(k, s) for k, s in tasks])
        for item in results:
            if not item:
                continue
            kind, data = item
            if kind == "doc":
                if google_doc is None:
                    google_doc = data
                else:
                    prev = google_doc
                    google_doc = [prev, data] if not isinstance(prev, list) else [*prev, data]
            else:
                google_sheets.append(data)

    return {"google_doc": google_doc, "google_sheets": google_sheets}


async def fetch_roadmap_from_google(sources: list[ProjectSource]) -> dict[str, Any]:
    bound = await collect_roadmap_bound(sources)
    if not bound["google_doc"] and not bound["google_sheets"]:
        return {
            "columns": [],
            "rows": [],
            "meta": {
                "quarter": current_quarter_label(),
                "warnings": ["Нет источников Google Doc или Google Sheet у проекта"],
                "sources_used": [],
                "rows_returned": 0,
            },
            "saved": False,
        }
    data = import_roadmap_from_google(bound["google_doc"], bound["google_sheets"])
    data["saved"] = False
    return data


async def fetch_and_save_roadmap(
    session: Session,
    project_id: uuid.UUID,
    sources: list[ProjectSource],
) -> dict[str, Any]:
    from app.services.roadmap_repository import roadmap_to_api_payload, save_project_roadmap

    preview = await fetch_roadmap_from_google(sources)
    if not preview.get("rows"):
        return preview

    record = save_project_roadmap(
        session,
        project_id,
        columns=preview["columns"],
        rows=preview["rows"],
        sources_used=preview["meta"].get("sources_used") or [],
        warnings=preview["meta"].get("warnings") or [],
    )
    payload = roadmap_to_api_payload(record)
    payload["meta"]["quarter_filter_applied"] = False
    return payload
