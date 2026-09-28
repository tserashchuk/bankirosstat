from __future__ import annotations

import re
import uuid
from typing import Any

from sqlmodel import Session

from app.models import InternalProject, ProjectSource, ReportHistory, SourceType
from app.repositories import get_project_with_sources
from app.services.llm.prompts import (
    REPORT_FORMAT_MILESTONE_GAMMA,
    REPORT_FORMAT_MILESTONE_SYNC,
    current_quarter_label,
    is_milestone_report_format,
    normalize_report_format,
)
from app.services.llm.youtrack_links import merge_youtrack_link_rules
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

_GAMMA_MILESTONE_DECK_STRUCTURE = """
## Цель
Синхронизация с milestones: динамика, сроки, сторона действия, партнёры, риски.

## Оформление
- Сохрани корпоративный шаблон; roadmap-слайды заполни milestones (без Google-roadmap).
- Мало текста: короткие тезисы, без RAG-светофора, эмодзи-статусов и цветовой легенды.
- Статусы только словами (достигнут / в работе / риск / просрочен / нет подтверждения), без цветовой RAG-разметки.
- Можно небольшие иллюстрации (иконка/мини-картинка на слайд), без крупных коллажей.
- По возможности: отдельный слайд с **диаграммой Ганта** по milestones и слайд партнёров (1–2 доп. карточки ок).

## Содержание (по отчёту DeepSeek)
1. Титул — проект, период.
2. Динамика — 3–5 коротких тезисов.
3. **Диаграмма Ганта** по milestones — отдельный слайд; горизонтальные полосы по датам из таблицы «Данные для диаграммы Ганта»; подписи: код, дедлайн, сторона. Без цветовой RAG-разметки. Если Gamma не строит Gantt — timeline с теми же датами, но сначала попробуй именно диаграмму Ганта.
4. Сводка — таблица: код | название | статус | дедлайн | сторона | риск.
5. Разбор точек (2–3 на слайд): статус, дедлайн, сторона, риск, шаги, ссылки YouTrack.
6. Партнёры — таблица из раздела «Статусы партнёров»; без выдуманных имён. Если партнёров нет — одна фраза.
7. Ближайшие 14 дней / просрочки.
8. Фокус на неделю — коротко, с кодами M и партнёрами.

Если в отчёте есть раздел «План презентации для Gamma» — это **рекомендация, не правило**:
можно упростить, объединить слайды, сменить порядок. Опора на факты разделов 1–5 важнее плана.

Только факты из отчёта и эталона. Текст на русском. Проекты портфеля не смешивать.
"""

_GAMMA_MILESTONE_PORTFOLIO_EXTRA = """
Портфель: общее → по проектам (динамика → диаграмма Ганта → точки → партнёры → фокус).
Без milestones — один слайд «точки не заданы».
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
    """Сначала из актуального generated_text (после правок), snapshot — запасной вариант."""
    m = re.search(
        r"##\s*📋\s*Саммари(?:\s+по\s+портфелю)?\s*\n+(.*?)(?=\n---|\Z)",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    m = re.search(r"##\s*📋\s*Саммари\s*\n+(.*?)(?=\n---|\Z)", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    stored = (snapshot.get("portfolio_summary") or "").strip()
    return stored


def _project_report_body(
    entry: dict[str, Any],
    parsed_sections: dict[str, str],
) -> str:
    """Текст проекта из отредактированного отчёта; snapshot.report_text — только fallback."""
    if entry.get("error"):
        return f"_Ошибка генерации: {entry['error']}_"
    name = (entry.get("project_name") or "").strip()
    if name in parsed_sections:
        return parsed_sections[name]
    for key, body in parsed_sections.items():
        if name and (name in key or key in name):
            return body
    stored = (entry.get("report_text") or "").strip()
    if stored:
        return stored
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


def infer_gamma_report_format(report: ReportHistory) -> str:
    """Формат отчёта для выбора промпта Gamma. По умолчанию — текущий (не milestone)."""
    snap = report.raw_data_snapshot or {}
    stored = snap.get("report_format")
    if stored:
        return normalize_report_format(str(stored))
    text = report.generated_text or ""
    if "План презентации для Gamma" in text or "Milestone + план презы" in text:
        return REPORT_FORMAT_MILESTONE_GAMMA
    if "Синхронизация с milestone" in text:
        return REPORT_FORMAT_MILESTONE_SYNC
    if re.search(r"^###\s+M\d+", text, re.M) and "Следующие шаги" in text:
        return REPORT_FORMAT_MILESTONE_SYNC
    return normalize_report_format(None)


def gamma_num_cards_for_report(report: ReportHistory, requested: int) -> int:
    """Для milestone увеличиваем число слайдов, если фронт прислал дефолт 10."""
    fmt = infer_gamma_report_format(report)
    if not is_milestone_report_format(fmt) or int(requested) != 10:
        return max(4, min(25, int(requested)))
    snap = report.raw_data_snapshot or {}
    n = 0
    for entry in snap.get("projects") or []:
        inner = entry.get("snapshot") if isinstance(entry, dict) else None
        if isinstance(inner, dict):
            n += len(inner.get("milestones") or [])
    if not n and isinstance(snap.get("milestones"), list):
        n = len(snap["milestones"])
    suggested = 10 + max(n, 4)
    return max(14, min(25, suggested))


def _youtrack_issue_url(base: str, issue_id: str) -> str:
    return f"{(base or '').rstrip('/')}/issue/{issue_id}"


def _youtrack_links_block(snapshot: dict[str, Any] | None) -> str:
    """Компактный список задач со ссылками — чтобы Gamma делала кликабельные ID."""
    if not snapshot:
        return ""
    snapshots = [snapshot]
    for entry in snapshot.get("projects") or []:
        if isinstance(entry, dict) and isinstance(entry.get("snapshot"), dict):
            snapshots.append(entry["snapshot"])
    rules = merge_youtrack_link_rules(*snapshots)
    by_prefix = {r.prefix: r.base_url for r in rules}

    lines: list[str] = []
    seen: set[str] = set()
    for snap in snapshots:
        for block in snap.get("youtrack") or []:
            if not isinstance(block, dict):
                continue
            base = str(block.get("instance_url") or "").strip().rstrip("/")
            for issue in block.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                issue_id = str(issue.get("id") or "").strip()
                if not issue_id or issue_id in seen:
                    continue
                seen.add(issue_id)
                prefix = issue_id.split("-", 1)[0].upper()
                url_base = by_prefix.get(prefix) or base
                summary = (issue.get("summary") or "").strip()
                state = (issue.get("state") or "").strip()
                label = f"{issue_id}: {summary}" if summary else issue_id
                if state:
                    label += f" — {state}"
                if url_base:
                    lines.append(f"- [{issue_id}]({_youtrack_issue_url(url_base, issue_id)}) — {label}")
                else:
                    lines.append(f"- {label}")
                if len(lines) >= 40:
                    break
            if len(lines) >= 40:
                break
        if len(lines) >= 40:
            break
    if not lines:
        return ""
    return "\n".join(["### Задачи YouTrack (для кликабельных ссылок)", ""] + lines)


def _milestones_etalon_block(snapshot: dict[str, Any] | None, *, heading: str = "Эталон milestones") -> str:
    if not snapshot:
        return f"### {heading}\n\n_Нет эталона._"
    rows = snapshot.get("milestones") if isinstance(snapshot.get("milestones"), list) else []
    dict_rows = [r for r in rows if isinstance(r, dict)]
    if not dict_rows:
        return f"### {heading}\n\nMilestones не заданы."
    lines = [f"### {heading} ({len(dict_rows)})", ""]
    for row in dict_rows:
        code = (row.get("code") or "").strip() or "—"
        title = (row.get("title") or "").strip() or "без названия"
        due = (row.get("deadline_label") or row.get("deadline") or "").strip() or "без даты"
        until = row.get("days_until_deadline")
        if isinstance(until, int):
            timing = f"просрочен на {abs(until)} дн." if until < 0 else f"через {until} дн."
        else:
            timing = ""
        criterion = (row.get("description") or "").strip()
        if len(criterion) > 220:
            criterion = criterion[:220] + "…"
        extra = f" ({timing})" if timing else ""
        line = f"- {code} — {title} — дедлайн {due}{extra}"
        if criterion:
            line += f" — критерий: {criterion}"
        lines.append(line)
    return "\n".join(lines)


def _iso_date(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def _milestones_gantt_block(snapshot: dict[str, Any] | None, *, heading: str = "Данные для диаграммы Ганта") -> str:
    """Таблица дат для Gamma: полоса = от конца предыдущей точки (или as_of) до дедлайна."""
    if not snapshot:
        return f"### {heading}\n\n_Нет данных для Ганта._"
    rows = snapshot.get("milestones") if isinstance(snapshot.get("milestones"), list) else []
    dict_rows = [r for r in rows if isinstance(r, dict)]
    if not dict_rows:
        return f"### {heading}\n\nMilestones не заданы — слайд с диаграммой Ганта опусти."
    meta = snapshot.get("milestones_meta") if isinstance(snapshot.get("milestones_meta"), dict) else {}
    as_of = _iso_date(meta.get("as_of")) or "—"
    lines = [
        f"### {heading}",
        "",
        f"as_of: {as_of}. Старт полосы = дедлайн предыдущего M (или as_of); конец = дедлайн.",
        "Построй **диаграмму Ганта** (Gantt chart) по этой таблице. Статус/сторону — из отчёта DeepSeek.",
        "",
        "| Код | Название | Начало | Конец | Дней до дедлайна |",
        "|---|---|---|---|---|",
    ]
    prev_end = as_of if as_of != "—" else ""
    for row in dict_rows:
        code = (row.get("code") or "").strip() or "—"
        title = (row.get("title") or "").strip() or "без названия"
        end = _iso_date(row.get("deadline")) or (row.get("deadline_label") or "").strip() or "нет даты"
        start = prev_end or as_of
        if (
            len(start) == 10
            and len(str(end)) == 10
            and str(end)[4] == "-"
            and start > str(end)
        ):
            start = str(end)
        until = row.get("days_until_deadline")
        until_s = str(until) if isinstance(until, int) else "—"
        lines.append(f"| {code} | {title} | {start or '—'} | {end} | {until_s} |")
        if _iso_date(row.get("deadline")):
            prev_end = _iso_date(row.get("deadline"))
    return "\n".join(lines)


def build_gamma_milestone_presentation_prompt(
    *,
    report_date: str,
    combined_report_text: str,
    snapshot: dict[str, Any],
    from_template: bool = False,
) -> str:
    """Промпт Gamma для формата «Синхронизация с milestone»: без roadmap."""
    parts: list[str] = []
    if from_template:
        parts.append(
            "Заполни корпоративный шаблон под синхронизацию с milestone: вместо roadmap — "
            "milestones (статус, дедлайн, сторона, риск, шаги). Мало текста, без RAG-цветов. "
            "Можно небольшие картинки. По возможности — слайд с **диаграммой Ганта** и слайд партнёров. "
            "Если в отчёте есть «План презентации для Gamma» — это рекомендация, не правило."
        )
    else:
        parts.append(
            "Презентация на русском: синхронизация с milestone. Кратко, без RAG-светофора; "
            "можно небольшие картинки; по возможности слайд с **диаграммой Ганта** и слайд партнёров. "
            "План презы из отчёта — рекомендация, не правило."
        )
    parts.append(_GAMMA_MILESTONE_DECK_STRUCTURE.strip())

    projects = snapshot.get("projects") or []
    is_portfolio = bool(snapshot.get("portfolio") and len(projects) > 1)
    if is_portfolio:
        parts.append(_GAMMA_MILESTONE_PORTFOLIO_EXTRA.strip())

    parts.extend(
        [
            "",
            "---",
            "## Исходные данные",
            f"Дата отчёта: {report_date}",
            "Формат: Синхронизация с milestone. Без Google-roadmap.",
        ]
    )

    if is_portfolio:
        summary = _extract_portfolio_summary_from_text(combined_report_text, snapshot)
        parts.extend(["", "---", "## Саммари по портфелю", ""])
        parts.append(summary or "Саммари нет — собери динамику из блоков проектов.")
        parsed = _parse_project_sections_from_combined_text(combined_report_text)
        for idx, entry in enumerate(projects, start=1):
            if not isinstance(entry, dict):
                continue
            name = (entry.get("project_name") or f"Проект {idx}").strip()
            inner = entry.get("snapshot") if isinstance(entry.get("snapshot"), dict) else {}
            parts.extend(["", "---", f"## Проект {idx}: {name}", ""])
            parts.append("### Отчёт DeepSeek по этому проекту")
            parts.append(_project_report_body(entry, parsed) or "_Нет текста отчёта._")
            parts.extend(["", _milestones_etalon_block(inner, heading=f"Эталон milestones «{name}»")])
            parts.extend(["", _milestones_gantt_block(inner, heading=f"Данные для диаграммы Ганта «{name}»")])
            yt = _youtrack_links_block(inner)
            if yt:
                parts.extend(["", yt])
    else:
        parts.extend(
            [
                "",
                "---",
                "## Текст отчёта DeepSeek (источник статусов, дедлайнов и следующих шагов)",
                combined_report_text,
            ]
        )
        inner = snapshot
        if projects and isinstance(projects[0], dict) and isinstance(projects[0].get("snapshot"), dict):
            inner = projects[0]["snapshot"]
            if not combined_report_text.strip() and projects[0].get("report_text"):
                parts.append(str(projects[0].get("report_text")))
        parts.extend(["", _milestones_etalon_block(inner)])
        parts.extend(["", _milestones_gantt_block(inner)])
        yt = _youtrack_links_block(snapshot)
        if yt:
            parts.extend(["", yt])

    return "\n".join(parts)


def build_gamma_prompt_for_report(
    session: Session,
    report: ReportHistory,
    *,
    sync_tasks_block: str = "",
    from_template: bool = False,
) -> str:
    """Собирает промпт Gamma: свой шаблон на формат отчёта; milestone — без roadmap."""
    text = (report.generated_text or "").strip()
    report_date = report.created_at.strftime("%d.%m.%Y")
    snap = report.raw_data_snapshot or {}
    fmt = infer_gamma_report_format(report)

    if is_milestone_report_format(fmt):
        return build_gamma_milestone_presentation_prompt(
            report_date=report_date,
            combined_report_text=text,
            snapshot=snap,
            from_template=from_template,
        )

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
