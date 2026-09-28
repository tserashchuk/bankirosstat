import unittest

from app.models import RagStatus
from app.services.llm.generator import infer_rag_status
from app.services.llm.prompts import (
    REPORT_FORMAT_MILESTONE_SYNC,
    build_user_prompt,
    get_system_prompt,
)


class TestDeepSeekReasonerGuards(unittest.TestCase):
    def test_empty_text_is_not_green(self):
        self.assertEqual(infer_rag_status(""), RagStatus.AMBER)
        self.assertEqual(infer_rag_status("   "), RagStatus.AMBER)
        self.assertEqual(infer_rag_status("коротко"), RagStatus.AMBER)

    def test_explicit_rag_still_works(self):
        self.assertEqual(infer_rag_status("RAG_STATUS: GREEN\n### M1"), RagStatus.GREEN)

    def test_milestone_prompt_drops_roadmap(self):
        snap = {
            "milestones": [
                {
                    "code": "M1",
                    "title": "Модель",
                    "deadline": "2026-08-28",
                    "deadline_label": "28.08.2026",
                    "days_until_deadline": 2,
                    "overdue": False,
                }
            ],
            "milestones_meta": {"count": 1, "as_of": "2026-08-26"},
            "roadmap_table": [{"initiative": "X", "status": "todo"}],
            "youtrack": [{"issues": [{"id": "KR-1", "summary": "x"}]}],
        }
        user = build_user_prompt("Криптокарта", snap, REPORT_FORMAT_MILESTONE_SYNC)
        self.assertIn("MILESTONES_COUNT: 1", user)
        self.assertNotIn('"roadmap_table"', user)
        self.assertIn(
            "НЕ означает отсутствие milestones",
            get_system_prompt(REPORT_FORMAT_MILESTONE_SYNC),
        )


if __name__ == "__main__":
    unittest.main()
