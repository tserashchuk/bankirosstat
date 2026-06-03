from __future__ import annotations

import re
import uuid
from typing import Any

from sqlmodel import Session

from app.models import InternalProject, ProjectSource, ReportHistory, SourceType
from app.repositories import get_project_with_sources
from app.services.llm.prompts import current_quarter_label
from app.services.roadmap_normalize import rows_to_llm_table
from app.services.roadmap_repository import get_project_roadmap
from app.services.source_creds import decrypt_normalized

_GAMMA_DECK_STRUCTURE = """
## Цель презентации
Отчётная презентация для руководства и учредителей: за 5 минут должно быть понятно,
(1) какие **результаты достигнуты** за отчётный период,
(2) **куда движется продукт** по плану на квартал,
(3) насколько **факт недели совпадает с roadmap** (в плане / отстаём / опережаем).

## Обязательная логика слайдов (сохрани порядок смысла, адаптируй под шаблон)
1. **Титул** — продукт/проект, период отчёта.
2. **Резюме** — 3–5 тезисов: главные итоги недели одной строкой каждый.
3. **Достигнутые результаты** — только факты из текста отчёта: что сделано, закрыто, запущено, измеримый эффект. Без воды и без выдумок.
4. **Куда движется продукт** — roadmap текущего квартала: инициативы, цели, статусы, сроки, ответственные. Визуально: таблица, дорожная карта или карточки по инициативам.
5. **План vs факт** — для каждой ключевой инициативы из roadmap (если есть данные): ожидание по плану → что произошло на неделе (из отчёта) → оценка: в плане / отстаём / опережаем / нет данных.
6. **Риски и фокус на следующую неделю** — из отчёта; при расхождении с roadmap — явно назвать.
7. **Полный roadmap** — отдельный слайд или QR/кнопка со **кликабельной ссылкой** на документ roadmap (см. блок Roadmap ниже).

Правила:
- Весь текст на русском.
- Не придумывай инициативы, метрики и результаты — только отчёт и roadmap ниже.
- Если roadmap пуст — блоки 4–5 сократи, укажи «план в документе по ссылке».
- Связывай формулировки: «результат недели X подтверждает инициативу Y roadmap».
"""

_GAMMA_PORTFOLIO_DECK_STRUCTURE = """
## Цель презентации (портфель проектов)
Один документ содержит **несколько независимых проектов**. У каждого проекта:
- **свой** еженедельный отчёт (факты недели),
- **свой** roadmap на текущий квартал (отдельный план и ссылка на документ),
- **свои** задачи синка (если приложены к проекту).

**Не смешивай проекты:** инициативы, задачи, результаты и roadmap одного проекта нельзя относить к другому.

В начале данных — **саммари по всему портфелю** (общая картина). Далее — **раздельные блоки по каждому проекту**.

## Обязательная логика слайдов
### Часть A — портфель (общее)
1. **Титул** — портфель проектов, период отчёта.
2. **Саммари по портфелю** — только из блока «Саммари по портфелю»: 4–6 тезисов, риски, что требует внимания учредителей.
3. **Сводная таблица / карта портфеля** — проект → статус (🟢/🟡/🔴) → главный итог недели → главный риск (по данным каждого проекта).

### Часть B — по каждому проекту (повтори структуру для КАЖДОГО проекта отдельной секцией)
Для проекта «{имя}»:
4. **Заголовок проекта** — название + статус недели.
5. **Результаты проекта** — только из отчёта этого проекта.
6. **Куда движется продукт (roadmap проекта)** — только roadmap этого проекта за текущий квартал.
7. **План vs факт (проект)** — сверка roadmap этого проекта с фактами из его отчёта.
8. **Фокус и риски (проект)** — из отчёта и задач синка этого проекта.
9. **Ссылка на полный roadmap проекта** — кликабельная ссылка из блока roadmap этого проекта.

Правила:
- Весь текст на русском.
- Саммари портфеля ≠ отчёт одного проекта: не дублируй дословно, обобщай.
- Roadmap проекта A не использовать для проекта B.
- Не выдумывай данные — только приложенные блоки.
"""


def _parse_document_id(raw: str) -> str:
    raw = (raw or "").strip()
    m = re.search(r"/document/d/([a-zA-Z0-9-_]+)", raw)
    if m:
        return m.group(1)
    return raw


def _parse_spreadsheet_id(raw: str) -> str:
    raw = (raw or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", raw)
    if m:
        return m.group(1)
    return raw


def collect_roadmap_document_links(sources: list[ProjectSource]) -> list[dict[str, str]]:
    """Публичные ссылки на Google Doc/Sheet roadmap (Doc — приоритетнее)."""
    docs: list[dict[str, str]] = []
    sheets: list[dict[str, str]] = []
    for src in sources:
        if src.source_type == SourceType.GOOGLE_DOC:
            creds = decrypt_normalized(src.credentials_encrypted)
            doc_id = _parse_document_id(str(creds.get("document_id") or ""))
            if not doc_id:
                continue
            label = (src.label or "Roadmap (Google Doc)").strip()
            docs.append(
                {
                    "label": label,
                    "url": f"https://docs.google.com/document/d/{doc_id}/edit",
                }
            )
        elif src.source_type == SourceType.GOOGLE_SHEET:
            creds = decrypt_normalized(src.credentials_encrypted)
            sheet_id = _parse_spreadsheet_id(str(creds.get("spreadsheet_id") or ""))
            if not sheet_id:
                continue
            gid = str(creds.get("gid") or "0").strip() or "0"
            label = (src.label or "Roadmap (Google Sheets)").strip()
            sheets.append(
                {
                    "label": label,
                    "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#gid={gid}",
                }
            )
    return docs + sheets


def _format_row_line(idx: int, row: dict[str, Any]) -> str:
    initiative = (row.get("initiative") or "").strip()
    if not initiative:
        return ""
    bits = [initiative]
    status = (row.get("status") or "").strip()
    due = (row.get("due") or "").strip()
    owner = (row.get("owner") or "").strip()
    quarter = (row.get("quarter") or "").strip()
    comment = (row.get("comment") or "").strip()
    if status:
        bits.append(f"статус в плане: {status}")
    if due:
        bits.append(f"срок: {due}")
    if owner:
        bits.append(f"ответственный: {owner}")
    if quarter:
        bits.append(f"квартал: {quarter}")
    if comment:
        bits.append(f"комментарий: {comment}")
    return f"{idx}. " + " — ".join(bits)


def build_gamma_roadmap_block(session: Session, project_id: uuid.UUID) -> str:
    """Данные roadmap для промпта Gamma: куда движется продукт + ссылка на полный план."""
    quarter = current_quarter_label()
    _, sources = get_project_with_sources(session, project_id)
    doc_links = collect_roadmap_document_links(sources)
    roadmap = get_project_roadmap(session, project_id)

    lines = [
        "---",
        f"## Roadmap — куда движется продукт ({quarter})",
        "",
        (
            f"Это план на квартал {quarter}. Используй для слайдов «Куда движется продукт» и «План vs факт»: "
            "покажи траекторию, приоритеты и статусы инициатив так, чтобы было видно направление развития продукта."
        ),
    ]

    if doc_links:
        lines.extend(
            [
                "",
                "Кликабельная ссылка на полный документ roadmap (слайд «Полный roadmap»):",
            ]
        )
        for link in doc_links:
            lines.append(f"- {link['label']}: {link['url']}")
    else:
        lines.extend(
            [
                "",
                "Ссылка на полный документ roadmap не настроена в источниках проекта.",
            ]
        )

    if not roadmap or not roadmap.rows:
        lines.extend(
            [
                "",
                "Строки roadmap в базе не сохранены — не выдумывай инициативы; "
                "на слайде направления укажи, что актуальный план — в документе по ссылке (если есть).",
            ]
        )
        return "\n".join(lines)

    llm_rows, _meta = rows_to_llm_table(
        roadmap.columns or [],
        roadmap.rows or [],
        quarter_filter=True,
        max_rows=0,
    )
    if not llm_rows:
        lines.extend(
            [
                "",
                f"В базе нет строк roadmap за {quarter} — опирайся на полный документ по ссылке.",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            "",
            f"Инициативы плана на {quarter} (для визуализации и сверки с отчётом):",
            "",
        ]
    )
    n = 0
    for row in llm_rows:
        line = _format_row_line(n + 1, row)
        if line:
            n += 1
            lines.append(line)

    if n == 0:
        lines.append(f"(Нет названных инициатив за {quarter}.)")

    return "\n".join(lines)


def _parse_project_sections_from_combined_text(text: str) -> dict[str, str]:
    """Из объединённого markdown отчёта — текст по заголовкам ## Проект."""
    sections: dict[str, str] = {}
    chunks = re.split(r"\n---+\s*\n", text)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"^##\s+(.+?)\s*(?:[🟢🟡🔴])?\s*\n+(.*)", chunk, re.DOTALL)
        if not m:
            continue
        title = m.group(1).strip()
        if title.startswith("📋") or "Саммари" in title:
            continue
        body = m.group(2).strip()
        if body:
            sections[title] = body
    return sections


def _extract_portfolio_summary_from_text(text: str, snapshot: dict[str, Any]) -> str:
    stored = (snapshot.get("portfolio_summary") or "").strip()
    if stored:
        return stored
    m = re.search(
        r"##\s*📋\s*Саммари(?:\s+по\s+портфелю)?\s*\n+(.*?)(?=\n---|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    m = re.search(r"##\s*📋\s*Саммари\s*\n+(.*?)(?=\n---|\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _project_report_body(
    entry: dict[str, Any],
    parsed_sections: dict[str, str],
) -> str:
    stored = (entry.get("report_text") or "").strip()
    if stored:
        return stored
    if entry.get("error"):
        return f"_Ошибка генерации: {entry['error']}_"
    name = (entry.get("project_name") or "").strip()
    if name in parsed_sections:
        return parsed_sections[name]
    for key, body in parsed_sections.items():
        if name and (name in key or key in name):
            return body
    return ""


def build_gamma_portfolio_presentation_prompt(
    session: Session,
    *,
    report_date: str,
    combined_report_text: str,
    snapshot: dict[str, Any],
    sync_tasks_block: str = "",
    from_template: bool = False,
) -> str:
    """Промпт для портфельного отчёта: саммари + отдельные проекты с отдельными roadmap."""
    projects = snapshot.get("projects") or []
    if not projects:
        return build_gamma_presentation_prompt(
            project_name="Портфель",
            report_date=report_date,
            report_text=combined_report_text,
            from_template=from_template,
        )

    parts: list[str] = []
    if from_template:
        parts.append(
            "Заполни корпоративный шаблон презентации: сохрани структуру и оформление шаблона, "
            "замени только тексты и данные под портфельную логику ниже."
        )
    else:
        parts.append(
            "Создай отчётную презентацию (presentation) на русском языке для портфеля проектов."
        )
    parts.append(_GAMMA_PORTFOLIO_DECK_STRUCTURE.strip())
    parts.extend(
        [
            "",
            "---",
            "## Исходные данные (портфель)",
            f"Дата отчёта: {report_date}",
            f"Число проектов в портфеле: {len(projects)}",
            "",
            "Ниже данные разделены: сначала общее саммари, затем каждый проект со своим отчётом и своим roadmap.",
        ]
    )

    portfolio_summary = _extract_portfolio_summary_from_text(combined_report_text, snapshot)
    parts.extend(["", "---", "## Саммари по портфелю (общее по всем проектам)", ""])
    if portfolio_summary:
        parts.append(portfolio_summary)
    else:
        parts.append(
            "Саммари не сформировано — сделай краткую сводку по блокам проектов ниже, без выдумок."
        )

    parsed_sections = _parse_project_sections_from_combined_text(combined_report_text)
    quarter = current_quarter_label()

    for idx, entry in enumerate(projects, start=1):
        name = (entry.get("project_name") or f"Проект {idx}").strip()
        rag = (entry.get("rag_status") or "").strip()
        rag_label = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴"}.get(rag, "")
        report_body = _project_report_body(entry, parsed_sections)

        parts.extend(["", "---", f"## Проект {idx}: {name} {rag_label}".strip(), ""])
        parts.append(
            f"Это **отдельный проект** со своим отчётом и своим roadmap на {quarter}. "
            "Не смешивай с другими проектами."
        )
        parts.extend(["", f"### Отчёт проекта «{name}»", ""])
        parts.append(report_body or "_Нет текста отчёта по проекту._")

        raw_pid = entry.get("project_id")
        if raw_pid:
            try:
                pid = uuid.UUID(str(raw_pid))
                roadmap_block = build_gamma_roadmap_block(session, pid)
                parts.extend(["", roadmap_block])
                project = session.get(InternalProject, pid)
                desc = (project.description or "").strip() if project else ""
                if desc:
                    parts.extend(
                        [
                            "",
                            f"### Контекст проекта «{name}» (для классификации задач)",
                            desc,
                        ]
                    )
            except ValueError:
                parts.append("", f"### Roadmap проекта «{name}»", "", "_Некорректный project_id._")
        else:
            parts.extend(["", f"### Roadmap проекта «{name}»", "", "_project_id не указан._"])

    if sync_tasks_block:
        parts.extend(
            [
                "",
                "---",
                "## Задачи синков (портфель / отчёт)",
                "",
                "Привязывай задачи синка к проекту только если это явно следует из контекста; "
                "иначе вынеси в общий блок «прочее».",
                "",
                sync_tasks_block,
            ]
        )

    return "\n".join(parts)


def build_gamma_presentation_prompt(
    *,
    project_name: str,
    report_date: str,
    report_text: str,
    roadmap_block: str = "",
    sync_tasks_block: str = "",
    project_description: str = "",
    from_template: bool = False,
) -> str:
    """Единый промпт: один проект = результаты + направление (roadmap) + план vs факт."""
    parts: list[str] = []
    if from_template:
        parts.append(
            "Заполни корпоративный шаблон презентации: сохрани структуру и оформление шаблона, "
            "замени только тексты и данные под отчётную логику ниже."
        )
    else:
        parts.append(
            "Создай отчётную презентацию (presentation) на русском языке для руководства и учредителей."
        )
    parts.append(_GAMMA_DECK_STRUCTURE.strip())
    parts.extend(
        [
            "",
            "---",
            "## Исходные данные (один проект)",
            f"Проект / продукт: {project_name}",
            f"Дата отчёта: {report_date}",
            "",
            "Это отчёт по **одному** проекту (не портфель). Roadmap и факты относятся только к нему.",
            "",
            "### Текст еженедельного отчёта (источник фактов и результатов)",
            report_text,
        ]
    )
    if project_description.strip():
        parts.extend(
            [
                "",
                "### Контекст проекта (для разнесения задач и фактов)",
                project_description.strip(),
            ]
        )
    if roadmap_block:
        parts.extend(["", roadmap_block])
    elif from_template:
        parts.extend(
            [
                "",
                "---",
                "## Roadmap",
                "Roadmap для проекта не приложен — слайды про направление продукта сократи или опусти.",
            ]
        )
    if sync_tasks_block:
        parts.extend(["", sync_tasks_block])
    return "\n".join(parts)


def build_gamma_prompt_for_report(
    session: Session,
    report: ReportHistory,
    *,
    sync_tasks_block: str = "",
    from_template: bool = False,
) -> str:
    """Собирает промпт Gamma: портфель (N проектов) или один проект."""
    text = (report.generated_text or "").strip()
    report_date = report.created_at.strftime("%d.%m.%Y")
    snap = report.raw_data_snapshot or {}

    if snap.get("portfolio") and (snap.get("projects") or []):
        return build_gamma_portfolio_presentation_prompt(
            session,
            report_date=report_date,
            combined_report_text=text,
            snapshot=snap,
            sync_tasks_block=sync_tasks_block,
            from_template=from_template,
        )

    project_name = "Отчёт"
    project_description = ""
    if report.project_id:
        project = session.get(InternalProject, report.project_id)
        if project:
            project_name = project.name
            project_description = (project.description or "").strip()

    roadmap_block = ""
    if report.project_id:
        roadmap_block = build_gamma_roadmap_block(session, report.project_id)

    return build_gamma_presentation_prompt(
        project_name=project_name,
        report_date=report_date,
        report_text=text,
        roadmap_block=roadmap_block,
        sync_tasks_block=sync_tasks_block,
        project_description=project_description,
        from_template=from_template,
    )
