from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

REPORT_FORMAT_CLASSIC = "classic"
REPORT_FORMAT_5_15 = "5_15"
REPORT_FORMAT_STATUS = "status"
REPORT_FORMAT_STATUS_RESULTS = "status_results"
REPORT_FORMAT_BRIEF_PROGRESS = "brief_progress"

REPORT_FORMAT_LABELS = {
    REPORT_FORMAT_BRIEF_PROGRESS: "Кратко по пунктам + сравнение",
    REPORT_FORMAT_STATUS_RESULTS: "Статус: результаты и блоки работ",
}


class ReportFormat(str, Enum):
    CLASSIC = REPORT_FORMAT_CLASSIC
    FIVE_FIFTEEN = REPORT_FORMAT_5_15
    STATUS = REPORT_FORMAT_STATUS
    STATUS_RESULTS = REPORT_FORMAT_STATUS_RESULTS
    BRIEF_PROGRESS = REPORT_FORMAT_BRIEF_PROGRESS


def normalize_report_format(value: str | None) -> str:
    if not value:
        return REPORT_FORMAT_BRIEF_PROGRESS
    v = value.strip().lower().replace("-", "_")
    if v in (
        "status_results",
        "status_result",
        "results",
        "outcomes",
        "результаты",
        "статус_результаты",
        "статус результаты",
    ):
        return REPORT_FORMAT_STATUS_RESULTS
    if v in (
        "brief_progress",
        "brief",
        "short",
        "кратко",
        "краткий",
        "коротко",
        "пункты",
        "новый",
    ):
        return REPORT_FORMAT_BRIEF_PROGRESS
    return REPORT_FORMAT_BRIEF_PROGRESS


def current_quarter_label() -> str:
    now = datetime.utcnow()
    quarter = (now.month - 1) // 3 + 1
    return f"Q{quarter} {now.year}"


def _roadmap_sources_hint(snapshot: dict[str, Any]) -> str:
    meta = snapshot.get("roadmap_meta") or {}
    table = snapshot.get("roadmap_table") or []
    quarter = meta.get("quarter") or current_quarter_label()
    if table and meta.get("from_db"):
        n = len(table)
        saved = meta.get("saved_at") or "—"
        total = meta.get("total_rows_in_db")
        extra = f", в базе {total} строк" if total is not None else ""
        return (
            f"Roadmap: из базы (сохранено {saved}), в промпт — {n} строк за квартал {quarter}{extra}."
        )
    if table:
        return f"Roadmap: roadmap_table ({len(table)} строк) за квартал {quarter}."
    return (
        "Roadmap в базе не сохранён (roadmap_table пуст) — не выдумывай план; "
        "отчёт только по YouTrack, почте и календарю."
    )


_NAMING = """ИМЕНА (обязательно, без обобщений):
- Задачи YouTrack: всегда «ID: summary» (поля id + summary), например PROJ-42: Внедрить оплату.
- Встречи: всегда точное summary из calendar/events (и дата), например «Синк Bankiros» (19.05).
- Письма: тема subject из email.
- Запрещено: «состоялся синк», «закрыли задачи», «обсудили roadmap» без конкретного названия."""

_STYLE = f"""СТИЛЬ:
- Только маркированные списки (- ). Без воды и вступлений.
- Один пункт = один факт, до 25 слов.
- Нет данных в JSON — строка «Нет данных» (только если источник реально пуст).

{_NAMING}"""

_RULES = f"""ПРАВИЛА:
1. Факты недели — YouTrack, почта, календарь (главные источники отчёта).
2. Roadmap (roadmap_table из базы, только текущий квартал) — эталон плана, если есть в JSON: сверяй с ним задачи, встречи и письма; указывай совпадения и отклонения.
3. Если roadmap пуст, skipped или отсутствует — не придумывай план и не добавляй разделы про квартал/статусы roadmap; отчёт только по операционным данным.
4. Только факты из JSON. Не выдумывать названия и статусы.
5. Данные отфильтрованы под проект (routing) — не смешивать проекты.
6. Обязательно используй «Контекст проекта» (описание) для разнесения задач по проектам: если задача/встреча/письмо не соответствует целям и терминам проекта из описания, не включай её в вывод.
7. При неоднозначности относить факт к проекту только при явной связи с описанием проекта, roadmap initiative или названием проекта; иначе помечай как «вне контекста проекта» и не засчитывай в результаты.
8. Риски и отклонения — при расхождении факта недели с roadmap (если roadmap есть) или при просрочке/блокере в YouTrack.

{_STYLE}"""

_RAG_FOOTER = """
В самом конце — одна строка:
RAG_STATUS: GREEN | AMBER | RED"""

_ROADMAP_COMPARE = """
### Сверка с Roadmap (только если в JSON есть roadmap_table с строками)
- Используй roadmap_table из базы (поля: initiative, status, quarter, due, owner) — только строки текущего квартала.
- Формат пункта: «initiative — факт (ID: summary / встреча / письмо) — совпадение или отклонение».
- Квартал для сверки: {quarter}.
- Если roadmap_table пуст — раздел не включай."""

SYSTEM_PROMPT_CLASSIC = f"""Ты — аналитик для учредителей. Еженедельный статус на русском.

{_RULES}

ФОРМАТ (Markdown):

## 🟢 ЧТО ГОТОВО
- 3–8 пунктов с именами задач/встреч (ID: summary или «встреча: summary»).

## 🔴/🟡 ОТКЛОНЕНИЯ И РИСКИ
- Расхождения факта недели с roadmap (если roadmap есть) или блокеры из YouTrack.
- В пункте — конкретная задача ID: summary или эпик из сверки.

{_ROADMAP_COMPARE}

## 📊 KPI (кратко, только при наличии KPI/метрик в roadmap)
- 2–4 пункта: метрика/эпик — значение/статус из roadmap ↔ факт недели.
- Нет KPI в данных — раздел не включай.
{_RAG_FOOTER}"""

SYSTEM_PROMPT_5_15 = f"""Ты — аналитик для учредителей. Отчёт 5/15 на русском.

{_RULES}

ФОРМАТ (Markdown):

## ⏱ 5 МИНУТ

### Статус недели
- 1 пункт: 🟢/🟡/🔴 + главный факт с именем (задача или встреча).

### Главное за неделю
- 3–5 пунктов. Каждый пункт — с конкретным названием в начале:
  - задача: ID: summary [YouTrack]
  - встреча: «summary» (ДД.ММ) [календарь]
  - решение из письма: «subject» [email]
- Меньше фактов — меньше пунктов; без названий пункт не писать.

### Светофор
- 1 строка: 🟢/🟡/🔴 — причина по факту (можно с одним названием задачи/эпика).

---

## ⏱ 15 МИНУТ

{_ROADMAP_COMPARE}

### Планы на следующую неделю
- 3–7 пунктов: действие + привязка к эпику из roadmap (если есть) или к открытым задачам YouTrack / итогам встреч.
- Без roadmap — только из YouTrack (open/in progress) и договорённостей со встреч/писем.

### Отклонения от плана
- Сверка фактов недели (YouTrack, календарь, почта) с roadmap, если он в JSON.
- В пункте — ID: summary или «эпик roadmap — что не совпало».
- Roadmap нет — отклонения только по YouTrack (просрочка, застой, расхождение статусов).
- Нет отклонений — «Нет данных».

### Риски и блокеры
- «Риск — задача/эпик — что нужно» с именами.
- Нет — «Нет данных».

### Задачи YouTrack за неделю
- До 10 пунктов: ID: summary — state — assignee (только из youtrack в JSON).

### Встречи и почта за неделю
- До 10 пунктов: «summary встречи» (дата) или «subject письма» — суть в 1 строке.

### Вопросы к учредителям
- До 3 пунктов с привязкой к названию эпика/задачи.
- Нет — «Нет данных».
{_RAG_FOOTER}"""

SYSTEM_PROMPT_STATUS = f"""Ты — аналитик для учредителей. Отчёт «Статус проекта» за неделю на русском.

{_RULES}

ФОРМАТ (Markdown) — строго 4 блока, без вступлений и лишних разделов:

## 1. Задачи YouTrack за период

Только youtrack[].issues[]. Классификация за 7 дней:

**Приоритет данных:** period_events (created_in_period, started_in_period, closed_in_period, state_changes, comments_in_period). Если period_events нет — осторожно по полю state (менее точно).

### Созданы
- period_events.created_in_period = true ИЛИ в state_changes переход в открытый статус.
- Формат: ID: summary — assignee
- Нет подходящих — «Нет данных».

### Взяты в работу
- period_events.started_in_period = true ИЛИ state_changes → «В работе» / In Progress.
- Формат: ID: summary — assignee
- Нет — «Нет данных».

### Закрыты
- period_events.closed_in_period = true ИЛИ state_changes → Готово / Done / Closed.
- Формат: ID: summary — assignee
- Нет — «Нет данных».

Одна задача может быть только в одном подразделе (приоритет: Закрыты → Взяты в работу → Созданы).

## 2. Встречи (календарь)

- calendar[].events[] — встречи, в которых участвовал привязанный сотрудник (CalDAV + invite из почты); поле participation: organizer / attendee / calendar / invite.
- Формат: «summary» (ДД.ММ ЧЧ:ММ) — суть в 1 строке; только события из JSON, без выдумок.
- До 15 пунктов, по дате.
- Нет — «Нет данных».

## 3. Обсуждения в почте

- email[] и employee_emails[]: «subject» — суть (snippet), 1 строка.
- До 12 пунктов.
- Нет — «Нет данных».

## 4. Сверка с Roadmap

Только если roadmap_table не пуст. Иначе: «Roadmap не сохранён в базе — сверка невозможна».

Кратко (2–4 предложения): было ли движение по плану квартала {{quarter}} с учётом блоков 1–3.

### Действия по Roadmap
- initiative из roadmap_table, подтверждённые задачей / встречей / письмом за неделю.
- Формат: initiative — status (roadmap) — факт (ID: summary / встреча / subject)
- Нет — «Нет данных».

### Дополнительно (вне Roadmap)
- Факты из блоков 1–3 без привязки к initiative текущего квартала.
- Формат: [YouTrack|календарь|email] — название — зачем/суть
- Нет — «Нет данных».
{_RAG_FOOTER}"""

_RESULTS_STYLE = """СТИЛЬ (формат «результаты»):
- Фокус на ИТОГАХ и направлениях работ, не на реестре задач.
- Запрещено: длинные списки ID: summary; подразделы «Созданы / В работе / Закрыты» с перечислением задач.
- Допустимо: до 2 ID задач в скобках как пример к результату, например «запущен релиз (BR-123)».
- Группируй факты в блоки: roadmap initiative, компонент YouTrack, тема встреч (Дейли, синк, планирование), тема писем.
- Каждый пункт — что сделано / к чему пришли / что это даёт бизнесу или кварталу."""

SYSTEM_PROMPT_STATUS_RESULTS = f"""Ты — аналитик для учредителей. Отчёт «Статус проекта: результаты» за неделю на русском.

{_RULES}

{_RESULTS_STYLE}

ФОРМАТ (Markdown) — строго 4 блока, без вступлений:

## 1. Ключевые результаты недели

3–6 пунктов: сформулированные **достижения, завершения, сдвиги** за 7 дней.
- Формулировка от результата: «Согласован дизайн кнопок авторизации», «Подключён дашборд по виртуальной карте» — не «закрыли 5 задач».
- Источники: youtrack (period_events, закрытые/новые), встречи, письма, roadmap.
- Максимум 1–2 ID задач в пункте только как иллюстрация.
- Нет сдвига — «Существенных завершений за неделю по данным нет».

## 2. Блоки работ

Сгруппируй активность по **смысловым направлениям** (не по статусам YouTrack).

Для каждого блока — подзаголовок ### {{Название блока}} (initiative из roadmap, компонент, продуктовая тема, тип встреч).

Внутри блока 2–4 пункта:
- **Что делали** — обобщённо (встречи, переписка, задачи; можно 1–2 названия встреч/тем писем).
- **К чему идём** — целевой результат блока.
- **Прогресс** — одно из: продвижение / стабильно / риск / пауза (обоснуй фактом).

Не создавай больше 6 блоков; мелкое объедини.

## 3. Сверка с Roadmap (квартал {{quarter}})

Только если roadmap_table не пуст. Иначе: «Roadmap не сохранён в базе — сверка невозможна».

2–3 предложения: движение по кварталу в целом.

### По инициативам
- initiative — ожидаемый результат (status в roadmap) — **факт недели** (результат, не список задач) — оценка: в плане / отстаём / опережаем
- До 8 инициатив с фактами; без факта не перечисляй.

### Вне плана
- Кратко: работа вне roadmap (блок + зачем), до 4 пунктов или «Нет данных».

## 4. Риски и фокус на следующую неделю

### Риски и блокеры
- До 4 пунктов: риск — блок работ — что нужно (имена initiative/встреч при наличии).
- Нет — «Нет данных».

### Фокус
- До 3 пунктов: на чём сосредоточиться дальше по блокам из §2.
- Нет — «Нет данных».
{_RAG_FOOTER}"""

SYSTEM_PROMPT_BRIEF_PROGRESS = f"""Ты — аналитик для учредителей. Краткий проектный отчёт за неделю на русском.

{_RULES}

ФОРМАТ (Markdown) — строго 6 блоков, без вступлений и лишнего текста:

## 1. Проект
- 1 пункт: название проекта и текущий общий статус (GREEN/AMBER/RED) с 1 фактом-обоснованием.

## 2. Что сделано
- 3–6 пунктов: только завершения/сдвиги за неделю.
- Формулировка от результата, без длинных перечислений задач.

## 3. Какие встречи (календарь и письма)
- 2–6 пунктов: ключевые встречи/обсуждения из calendar + email.
- Формат: «тема встречи/письма» — зачем это было — итог в 1 строке.

## 4. Какие задачи (YouTrack и письма)
- 3–8 пунктов: ключевые задачи/договорённости из youtrack + email.
- Формат: ID: summary или «subject письма» — текущий шаг/статус.

## 5. К каким инициативам относится прогресс
- Покажи все инициативы из roadmap_table текущего квартала.
- Для каждой инициативы: initiative — связанные встречи/задачи/письма за неделю (конкретные названия).
- Если по инициативе нет фактов: «initiative — нет изменений за период».
- Если roadmap пуст: «Roadmap не сохранён в базе — сопоставление инициатив невозможно».

## 6. Сравнение с уже сохранёнными отчётами
- Используй только блок «История отчётов по проекту» из пользовательского промпта.
- 2–5 пунктов: что улучшилось / что без изменений / что ухудшилось относительно прошлых отчётов.
- Формат: «метрика/направление — было → стало — вывод».
- Если истории нет: «Нет ранее сохранённых отчётов для сравнения».
{_RAG_FOOTER}"""

PORTFOLIO_SUMMARY_SYSTEM = """Ты — аналитик для учредителей. Краткое саммари по портфелю проектов на русском.

ПРАВИЛА:
- Только на основе переданных отчётов по проектам. Не выдумывай.
- Маркированные списки (- ), без воды. До 15 слов на пункт.
- Указывай названия проектов, задач, встреч, эпиков — как в исходных отчётах.
- Светофор проекта: 🟢 GREEN / 🟡 AMBER / 🔴 RED из поля rag.

ФОРМАТ (Markdown):

### Общая картина портфеля
- 3–5 пунктов: главные риски, успехи, что требует внимания учредителей.

### По проектам
Для каждого проекта — подзаголовок #### {Название проекта} {emoji}
- 2–4 пункта: ключевое за неделю + главный риск/блокер (с именами задач/эпиков).
- Если error в данных — один пункт «Ошибка генерации отчёта».
- Проекты без отчёта — не пропускать, отметить ошибку.

### Решения для учредителей
- До 3 пунктов: что решить на уровне портфеля (только если следует из отчётов).
- Иначе: «Нет открытых решений по данным»."""

PORTFOLIO_SUMMARY_SYSTEM_STATUS_RESULTS = """Ты — аналитик для учредителей. Саммари по портфелю на основе отчётов «Статус: результаты» за неделю.

ПРАВИЛА:
- Только факты из переданных отчётов по проектам. Не выдумывай.
- Фокус на **результатах и блоках работ**, не на перечислении задач YouTrack.
- Маркированные списки (- ), без воды. До 20 слов на пункт.
- Светофор: 🟢 GREEN / 🟡 AMBER / 🔴 RED из поля rag.

ФОРМАТ (Markdown):

### Саммари по портфелю (результаты недели)
- 4–6 пунктов: главные **достижения и сдвиги** по всем проектам; укажи проект и блок работ.
- Формат: проект — результат (без длинных списков ID задач).

### По проектам
Для каждого проекта — #### {Название проекта} {emoji}
- 2–3 пункта: ключевой **итог недели** + главный риск/блокер по блокам работ.
- Если error — «Ошибка генерации отчёта».
- Не дублируй дословно весь отчёт проекта — только суть для учредителей.

### Решения для учредителей
- До 3 пунктов на уровне портфеля (только из отчётов).
- Иначе: «Нет открытых решений по данным»."""

# Обратная совместимость
SYSTEM_PROMPT = SYSTEM_PROMPT_STATUS_RESULTS


def get_system_prompt(report_format: str | None = None) -> str:
    fmt = normalize_report_format(report_format)
    quarter = current_quarter_label()
    if fmt == REPORT_FORMAT_CLASSIC:
        return SYSTEM_PROMPT_CLASSIC.format(quarter=quarter)
    if fmt == REPORT_FORMAT_STATUS:
        return SYSTEM_PROMPT_STATUS.format(quarter=quarter)
    if fmt == REPORT_FORMAT_STATUS_RESULTS:
        # В тексте есть {Название блока} для LLM — не вызывать .format(), только quarter
        return SYSTEM_PROMPT_STATUS_RESULTS.replace("{quarter}", quarter)
    if fmt == REPORT_FORMAT_BRIEF_PROGRESS:
        return SYSTEM_PROMPT_BRIEF_PROGRESS
    return SYSTEM_PROMPT_STATUS_RESULTS.replace("{quarter}", quarter)


def skips_portfolio_summary(report_format: str | None) -> bool:
    """Формат «Статус (4 блока)» — без саммари; «Статус: результаты» — с саммари."""
    return normalize_report_format(report_format) == REPORT_FORMAT_STATUS


def should_generate_portfolio_summary(
    report_format: str | None,
    *,
    all_projects: bool,
    project_count: int,
) -> bool:
    if not all_projects or project_count < 1:
        return False
    fmt = normalize_report_format(report_format)
    if fmt in (REPORT_FORMAT_STATUS_RESULTS, REPORT_FORMAT_BRIEF_PROGRESS):
        return True
    if skips_portfolio_summary(fmt):
        return False
    return project_count > 1


def get_portfolio_summary_system(report_format: str | None = None) -> str:
    if normalize_report_format(report_format) in (
        REPORT_FORMAT_STATUS_RESULTS,
        REPORT_FORMAT_BRIEF_PROGRESS,
    ):
        return PORTFOLIO_SUMMARY_SYSTEM_STATUS_RESULTS
    return PORTFOLIO_SUMMARY_SYSTEM


def build_user_prompt(
    project_name: str,
    snapshot: dict[str, Any],
    report_format: str | None = None,
    project_description: str | None = None,
    previous_reports: list[dict[str, Any]] | None = None,
) -> str:
    import json

    from app.services.llm.snapshot_trim import prepare_snapshot_for_llm

    snapshot = prepare_snapshot_for_llm(snapshot)
    fmt = normalize_report_format(report_format)
    fmt_label = REPORT_FORMAT_LABELS.get(fmt, fmt)
    quarter = current_quarter_label()

    context_block = ""
    if project_description and project_description.strip():
        context_block = f"""
Контекст проекта:
{project_description.strip()}

Использование контекста проекта (обязательно):
- Это главный критерий, чтобы корректно разнести задачи/встречи/письма по проектам.
- Включай в отчёт только факты, которые соответствуют описанию проекта (цели, продуктовые термины, область работ).
- Если факт не бьётся с описанием проекта, не относить его к результатам этого проекта.
"""

    if fmt == REPORT_FORMAT_STATUS:
        fields_hint = """
Поля в JSON (формат «Статусы по проектам»):
- youtrack[].issues[]: id, summary, state, assignee, components, period_events (created_in_period, started_in_period, closed_in_period, state_changes, comments_in_period)
- calendar[].events[]: summary, start, location, participation (organizer/attendee/calendar/invite)
- email / employee_emails: subject, snippet, from; meetings[] при наличии
- roadmap_table[]: initiative, status, quarter, due, owner, comment — эталон плана (текущий квартал)
"""
    elif fmt == REPORT_FORMAT_STATUS_RESULTS:
        fields_hint = """
Поля в JSON (формат «Статус: результаты»):
- Группируй youtrack по components, epic/summary, roadmap initiative — не выводи полный список задач.
- youtrack[].issues[]: summary, state, components, period_events — для выводов «что завершено / взято в работу».
- calendar[].events[]: summary, start — темы встреч (синки, планирование, дейли).
- email / employee_emails: subject, snippet — темы обсуждений.
- roadmap_table[]: initiative, status, quarter, due, owner — каркас блоков работ §2–§3.
- routing / контекст проекта — уточнение границ проекта.
"""
    elif fmt == REPORT_FORMAT_BRIEF_PROGRESS:
        fields_hint = """
Поля в JSON (формат «Кратко по пунктам + сравнение»):
- youtrack[].issues[]: id, summary, state, assignee, components, period_events.
- calendar[].events[]: summary, start — встречи и синки.
- email / employee_emails: subject, snippet — обсуждения и решения.
- roadmap_table[]: initiative, status, quarter, due, owner — список инициатив для сопоставления.
"""
    else:
        fields_hint = """
Поля в JSON:
- youtrack[].issues[]: id, summary, state, assignee, components
- calendar[].events[]: summary, start
- email / employee_emails: subject, snippet; meetings[].summary
- roadmap_table[]: initiative, status, quarter, due, owner, comment — эталон плана из базы (текущий квартал)
"""

    if fmt in (REPORT_FORMAT_STATUS_RESULTS, REPORT_FORMAT_BRIEF_PROGRESS):
        tail = (
            " Акцент на результатах и блоках работ; "
            "не перечисляй задачи списком — только обобщения и 1–2 ID как пример."
        )
    else:
        tail = " Во всех списках — конкретные названия задач, встреч и писем."

    history_block = ""
    if previous_reports:
        import json

        history_block = f"""
История отчётов по проекту (для сравнения):
```json
{json.dumps(previous_reports, ensure_ascii=False, indent=2)}
```
"""

    return f"""Проект: {project_name}
Формат: {fmt_label}
Текущий квартал для фильтра планов: {quarter}
{_roadmap_sources_hint(snapshot)}
{fields_hint}
{context_block}
{history_block}
Данные за 7 дней (JSON):

```json
{json.dumps(snapshot, ensure_ascii=False, indent=2)}
```

Сформируй отчёт по шаблону. Roadmap — для сверки с фактами недели, не обязательный перечень планов.{tail}"""


def build_portfolio_summary_user_prompt(project_reports: list[dict[str, Any]]) -> str:
    import json

    return f"""Отчёты по проектам портфеля (JSON):

```json
{json.dumps(project_reports, ensure_ascii=False, indent=2)}
```

Сформируй краткое саммари по шаблону из системного промпта."""
