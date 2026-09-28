from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app.config import get_settings

_STRING_KEYS = frozenset(
    {
        "body",
        "snippet",
        "text",
        "content",
        "roadmap_text",
        "sheet_csv",
        "description",
        "html",
        "raw",
    }
)


def _max_context_chars() -> int:
    try:
        return max(20_000, int(get_settings().llm_max_context_chars))
    except (TypeError, ValueError):
        return 80_000


def _max_field_chars() -> int:
    try:
        return max(500, int(get_settings().llm_max_field_chars))
    except (TypeError, ValueError):
        return 4_000


def _trim_string_fields(obj: Any, max_field: int) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(v, str) and (k in _STRING_KEYS or len(v) > max_field):
                if len(v) > max_field:
                    out[k] = v[:max_field] + "\n… (обрезано)"
                else:
                    out[k] = v
            else:
                out[k] = _trim_string_fields(v, max_field)
        return out
    if isinstance(obj, list):
        return [_trim_string_fields(x, max_field) for x in obj]
    return obj


def _cap_lists(obj: Any, limits: dict[str, int]) -> Any:
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in limits and isinstance(v, list) and len(v) > limits[k]:
                out[k] = v[: limits[k]]
                if len(v) > limits[k]:
                    out[f"_{k}_truncated"] = len(v) - limits[k]
            elif isinstance(v, (dict, list)):
                out[k] = _cap_lists(v, limits)
            else:
                out[k] = v
        return out
    if isinstance(obj, list):
        return [_cap_lists(x, limits) for x in obj]
    return obj


def _compact_roadmap_table_for_llm(data: dict[str, Any]) -> None:
    """В промпт — только канонические поля roadmap, без полного cells."""
    table = data.get("roadmap_table")
    if not isinstance(table, list):
        return
    compact: list[dict[str, Any]] = []
    for row in table:
        if not isinstance(row, dict):
            continue
        compact.append({k: v for k, v in row.items() if k != "cells"})
    data["roadmap_table"] = compact


def prepare_snapshot_for_llm(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Уменьшает JSON для промпта — снижает риск обрыва соединения с Gemini."""
    max_field = _max_field_chars()
    data = _trim_string_fields(deepcopy(snapshot), max_field)
    _compact_roadmap_table_for_llm(data)
    if isinstance(data.get("roadmap_table"), list) and len(data["roadmap_table"]) > 80:
        extra = len(data["roadmap_table"]) - 80
        data["roadmap_table"] = data["roadmap_table"][:80]
        data.setdefault("roadmap_meta", {})["llm_rows_truncated"] = extra
    data = _cap_lists(
        data,
        {
            "issues": 100,
            "events": 50,
            "messages": 40,
            "emails": 40,
            "meetings": 40,
            "roadmap_table": 80,
            "milestones": 40,
        },
    )

    # Milestones — эталон: не выкидывать при обрезке контекста.
    milestones_keep = data.get("milestones")
    milestones_meta_keep = data.get("milestones_meta")

    max_chars = _max_context_chars()
    raw = json.dumps(data, ensure_ascii=False)
    if len(raw) <= max_chars:
        return data

    trimmed = dict(data)
    list_keys = (
        "email",
        "employee_emails",
        "youtrack",
        "calendar",
        "issues",
        "events",
        "roadmap_table",
    )
    for key in list_keys:
        if key not in trimmed:
            continue
        val = trimmed[key]
        if not isinstance(val, list):
            continue
        while len(json.dumps(trimmed, ensure_ascii=False)) > max_chars and val:
            val.pop()
            trimmed[f"_{key}_dropped_for_llm"] = trimmed.get(f"_{key}_dropped_for_llm", 0) + 1

    if milestones_keep is not None:
        trimmed["milestones"] = milestones_keep
    if milestones_meta_keep is not None:
        trimmed["milestones_meta"] = milestones_meta_keep

    if len(json.dumps(trimmed, ensure_ascii=False)) > max_chars:
        trimmed["_llm_context_note"] = (
            f"Данные обрезаны до {max_chars} символов для запроса к ИИ."
        )

    return trimmed
