from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from app.services.source_creds import decrypt_normalized

MAX_SHEET_CHARS = 80_000


def _parse_spreadsheet_id(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", raw)
    if m:
        return m.group(1)
    return raw


def _export_url(spreadsheet_id: str, gid: str, fmt: str = "csv") -> str:
    base = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export"
    return f"{base}?format={fmt}&gid={gid}"


async def collect_google_sheet(credentials_encrypted: str) -> dict[str, Any]:
    creds = decrypt_normalized(credentials_encrypted)
    spreadsheet_id = _parse_spreadsheet_id(creds.get("spreadsheet_id", ""))
    gid = str(creds.get("gid") or "0").strip() or "0"
    sheet_name = creds.get("sheet_name")

    url = _export_url(spreadsheet_id, gid, "csv")

    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        text = resp.text

    if len(text) > MAX_SHEET_CHARS:
        text = text[:MAX_SHEET_CHARS] + "\n… (обрезано по лимиту контекста)"

    return {
        "source": "google_sheet",
        "spreadsheet_id": spreadsheet_id,
        "gid": gid,
        "sheet_name": sheet_name,
        "collected_at": datetime.utcnow().isoformat(),
        "sheet_csv": text,
        "hint": "Roadmap или KPI в табличном формате (CSV)",
    }


async def test_google_sheet_connection(credentials_encrypted: str) -> tuple[bool, str]:
    from app.models import SourceType
    from app.services.source_creds import missing_labels

    creds = decrypt_normalized(credentials_encrypted)
    missing = missing_labels(SourceType.GOOGLE_SHEET, creds)
    if missing:
        return False, f"Google Таблица: для проверки укажите {missing}"
    try:
        data = await collect_google_sheet(credentials_encrypted)
        if data.get("sheet_csv"):
            rows = data["sheet_csv"].strip().splitlines()
            preview = len(rows)
            return True, f"Google Таблица: доступна ({preview} строк CSV)"
        return False, "Google Таблица: пустой ответ"
    except Exception as e:
        return False, (
            f"Google Таблица: {e} — расшарьте таблицу «всем, у кому есть ссылка» на чтение"
        )
