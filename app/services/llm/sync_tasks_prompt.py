from __future__ import annotations

SYNC_TASKS_SYSTEM = """Из расшифровки синка извлеки action items.

ПРАВИЛА:
1. Только явные поручения из текста. Не выдумывай.
2. title — до 12 слов, глагол в начале («Согласовать…», «Подготовить…»). Без воды.
3. description — null или одна короткая строка контекста (до 15 слов). Не дублируй title.
4. assignee, due_date — только если названы в тексте; иначе null.
5. priority: high | medium | low.
6. Ответ — только JSON-массив, без markdown.

{"title": "...", "description": null, "assignee": null, "due_date": null, "priority": "medium"}
"""


def build_sync_tasks_user_prompt(project_name: str, meeting_title: str, transcript: str) -> str:
    title_part = meeting_title.strip() or "Созвон / синк"
    return f"""Проект: {project_name}
Встреча: {title_part}

Расшифровка:

{transcript.strip()}

Верни JSON-массив задач."""
