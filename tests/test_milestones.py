import unittest
from datetime import date

from app.services.llm.prompts import (
    REPORT_FORMAT_MILESTONE_GAMMA,
    REPORT_FORMAT_MILESTONE_SYNC,
    build_user_prompt,
    get_system_prompt,
    normalize_report_format,
)
from app.services.milestones import (
    days_until_deadline,
    deadline_label,
    parse_deadline,
)


class TestDeadlineParse(unittest.TestCase):
    def test_dmy_two_digit_year(self):
        self.assertEqual(parse_deadline("28.08.26"), "2026-08-28")

    def test_dmy_four_digit_year(self):
        self.assertEqual(parse_deadline("15.09.2026"), "2026-09-15")

    def test_iso(self):
        self.assertEqual(parse_deadline("2026-10-25"), "2026-10-25")

    def test_empty(self):
        self.assertIsNone(parse_deadline(""))
        self.assertIsNone(parse_deadline(None))

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_deadline("не дата")

    def test_label_and_overdue(self):
        self.assertEqual(deadline_label("2026-08-28"), "28.08.2026")
        today = date(2026, 8, 26)
        self.assertEqual(days_until_deadline("2026-08-28", today=today), 2)
        self.assertEqual(days_until_deadline("2026-08-20", today=today), -6)
        self.assertIsNone(days_until_deadline("", today=today))


class TestMilestoneReportFormat(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_report_format("milestone_sync"), REPORT_FORMAT_MILESTONE_SYNC)
        self.assertEqual(normalize_report_format("синхронизация"), REPORT_FORMAT_MILESTONE_SYNC)
        self.assertEqual(normalize_report_format("milestone_gamma"), REPORT_FORMAT_MILESTONE_GAMMA)
        self.assertEqual(normalize_report_format("план презы"), REPORT_FORMAT_MILESTONE_GAMMA)

    def test_system_prompt_mentions_milestones(self):
        prompt = get_system_prompt(REPORT_FORMAT_MILESTONE_SYNC)
        self.assertIn("MILESTONES_COUNT", prompt)
        self.assertIn("Следующие шаги", prompt)
        self.assertIn("Сторона следующего действия", prompt)
        self.assertIn("Риск:", prompt)
        self.assertIn("Статусы партнёров", prompt)
        self.assertIn("На стороне партнёра сейчас", prompt)

    def test_gamma_format_adds_presentation_plan(self):
        prompt = get_system_prompt(REPORT_FORMAT_MILESTONE_GAMMA)
        self.assertIn("План презентации для Gamma", prompt)
        self.assertIn("рекомендация", prompt.lower())
        self.assertIn("не правило", prompt.lower())
        self.assertIn("По каждому milestone", prompt)
        user = build_user_prompt(
            "Криптокарта",
            {
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
            },
            REPORT_FORMAT_MILESTONE_GAMMA,
        )
        self.assertIn("План презентации для Gamma", user)
        self.assertIn("рекомендация, не правило", user)

    def test_user_prompt_puts_etalon_first(self):
        snap = {
            "youtrack": [{"issues": [{"id": "KR-1", "summary": "x"}]}],
            "milestones": [
                {
                    "code": "M1",
                    "title": "Модель",
                    "description": "Согласована схема",
                    "deadline": "2026-08-28",
                    "deadline_label": "28.08.2026",
                    "days_until_deadline": 2,
                    "overdue": False,
                }
            ],
            "milestones_meta": {"count": 1, "as_of": "2026-08-26", "from_db": True},
        }
        user = build_user_prompt("Криптокарта", snap, REPORT_FORMAT_MILESTONE_SYNC)
        etalon_at = user.find("ЭТАЛОН MILESTONES")
        json_at = user.find("Данные за 7 дней")
        self.assertGreater(etalon_at, 0)
        self.assertGreater(json_at, etalon_at)
        self.assertIn("MILESTONES_COUNT: 1", user)
        self.assertIn("M1 | Модель", user)
        self.assertNotIn("MILESTONES_COUNT: 0", user)
        self.assertIn("статус по каждому найденному партнёру", user)


if __name__ == "__main__":
    unittest.main()
