from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from app.services.source_creds import decrypt_normalized


async def collect_google_doc(credentials_encrypted: str) -> dict[str, Any]:
    creds = decrypt_normalized(credentials_encrypted)
    doc_id = creds["document_id"]
    url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        text = resp.text

    return {
        "source": "google_doc",
        "document_id": doc_id,
        "collected_at": datetime.utcnow().isoformat(),
        "roadmap_text": text,
    }


async def test_google_doc_connection(credentials_encrypted: str) -> tuple[bool, str]:
    from app.models import SourceType
    from app.services.source_creds import missing_labels

    creds = decrypt_normalized(credentials_encrypted)
    missing = missing_labels(SourceType.GOOGLE_DOC, creds)
    if missing:
        return False, f"Google Doc: для проверки укажите {missing}"
    try:
        data = await collect_google_doc(credentials_encrypted)
        if data.get("roadmap_text"):
            return True, "Google Doc: документ доступен для чтения"
        return False, "Google Doc: пустой ответ"
    except Exception as e:
        return False, f"Google Doc: {e} — убедитесь, что документ расшарен «по ссылке»"
