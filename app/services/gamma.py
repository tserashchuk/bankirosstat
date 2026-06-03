from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings


def _headers() -> dict[str, str]:
    settings = get_settings()
    if not settings.gamma_api_key:
        raise ValueError("GAMMA_API_KEY не задан")
    return {
        "Content-Type": "application/json",
        "X-API-KEY": settings.gamma_api_key,
    }


def _base_url() -> str:
    return get_settings().gamma_base_url.rstrip("/")


def _parse_generation_id(data: dict[str, Any]) -> str:
    generation_id = str(data.get("generationId") or data.get("id") or "").strip()
    if not generation_id:
        raise RuntimeError("Gamma не вернул generationId")
    return generation_id


async def create_generation_from_template(
    *,
    gamma_id: str,
    prompt: str,
    title: str,
    export_as: str = "pptx",
) -> str:
    payload: dict[str, Any] = {
        "gammaId": gamma_id.strip(),
        "prompt": prompt,
        "title": title,
        "exportAs": export_as,
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        res = await client.post(
            f"{_base_url()}/generations/from-template",
            headers=_headers(),
            json=payload,
        )
    if res.status_code >= 400:
        detail = res.text
        raise RuntimeError(f"Gamma API error {res.status_code}: {detail}")
    return _parse_generation_id(res.json())


async def create_generation(
    *,
    input_text: str,
    title: str,
    num_cards: int = 10,
    export_as: str = "pptx",
    language: str | None = None,
) -> str:
    settings = get_settings()
    lang = (language or settings.gamma_language or "ru").strip().lower() or "ru"
    payload: dict[str, Any] = {
        "inputText": input_text,
        "title": title,
        "textMode": "condense",
        "format": "presentation",
        "numCards": max(4, min(25, int(num_cards))),
        "exportAs": export_as,
        "textOptions": {
            "language": lang,
            "tone": "деловой, конкретный",
            "audience": "руководство и учредители",
        },
        "additionalInstructions": (
            "Вся презентация строго на русском языке: заголовки, буллеты, подписи. "
            "Не переводить на английский. Кратко и по делу."
        ),
    }
    async with httpx.AsyncClient(timeout=90.0) as client:
        res = await client.post(
            f"{_base_url()}/generations",
            headers=_headers(),
            json=payload,
        )
    if res.status_code >= 400:
        detail = res.text
        raise RuntimeError(f"Gamma API error {res.status_code}: {detail}")
    return _parse_generation_id(res.json())


async def get_generation_status(generation_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        res = await client.get(
            f"{_base_url()}/generations/{generation_id}",
            headers=_headers(),
        )
    if res.status_code >= 400:
        detail = res.text
        raise RuntimeError(f"Gamma status error {res.status_code}: {detail}")
    data = res.json()
    return {
        "status": data.get("status"),
        "gamma_url": data.get("gammaUrl"),
        "export_url": data.get("exportUrl"),
        "generation_id": generation_id,
        "raw": data,
    }
