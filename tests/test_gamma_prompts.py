import unittest
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

from app.services.gamma_roadmap import (
    _milestones_gantt_block,
    build_gamma_milestone_presentation_prompt,
    build_gamma_prompt_for_report,
    gamma_num_cards_for_report,
    infer_gamma_report_format,
)
from app.services.llm.prompts import (
    REPORT_FORMAT_BRIEF_PROGRESS,
    REPORT_FORMAT_MILESTONE_GAMMA,
    REPORT_FORMAT_MILESTONE_SYNC,
)


def _report(**kwargs):
    snap = kwargs.pop("raw_data_snapshot", {})
    text = kwargs.pop("generated_text", "")
    return SimpleNamespace(
        id=uuid4(),
        project_id=None,
        generated_text=text,
        raw_data_snapshot=snap,
        created_at=datetime(2026, 8, 26, 12, 0, 0),
        **kwargs,
    )


class TestGammaMilestonePrompt(unittest.TestCase):
    def test_infer_from_snapshot_field(self):
        r = _report(raw_data_snapshot={"report_format": "milestone_sync"})
        self.assertEqual(infer_gamma_report_format(r), REPORT_FORMAT_MILESTONE_SYNC)

    def test_infer_gamma_plan_format(self):
        r = _report(raw_data_snapshot={"report_format": "milestone_gamma"})
        self.assertEqual(infer_gamma_report_format(r), REPORT_FORMAT_MILESTONE_GAMMA)
        r2 = _report(generated_text="## 6. План презентации для Gamma\n> Рекомендация")
        self.assertEqual(infer_gamma_report_format(r2), REPORT_FORMAT_MILESTONE_GAMMA)

    def test_infer_from_deepseek_heading(self):
        r = _report(generated_text="# Синхронизация с milestone (2)\n\n### M1 — Модель")
        self.assertEqual(infer_gamma_report_format(r), REPORT_FORMAT_MILESTONE_SYNC)

    def test_default_stays_brief(self):
        r = _report(generated_text="## 1. Проект\n- Криптокарта")
        self.assertEqual(infer_gamma_report_format(r), REPORT_FORMAT_BRIEF_PROGRESS)

    def test_milestone_prompt_has_no_roadmap_block(self):
        snap = {
            "portfolio": True,
            "report_format": "milestone_sync",
            "projects": [
                {
                    "project_name": "Криптокарта",
                    "rag_status": "AMBER",
                    "report_text": (
                        "### M1 — Модель\n"
                        "- **Дедлайн:** 28.08.2026 (через 2 дн.)\n"
                        "- **Статус:** в работе\n"
                        "- **Следующие шаги:** согласовать схему\n"
                        "- KR-78"
                    ),
                    "snapshot": {
                        "milestones": [
                            {
                                "code": "M1",
                                "title": "Модель",
                                "deadline_label": "28.08.2026",
                                "days_until_deadline": 2,
                                "description": "Согласована схема",
                            }
                        ],
                        "youtrack": [
                            {
                                "instance_url": "https://youtrack.myfin.group",
                                "issues": [
                                    {"id": "KR-78", "summary": "Пространства", "state": "В работе"}
                                ],
                            }
                        ],
                    },
                }
            ],
        }
        body = build_gamma_milestone_presentation_prompt(
            report_date="26.08.2026",
            combined_report_text="# Синхронизация с milestone\n\n" + snap["projects"][0]["report_text"],
            snapshot=snap,
        )
        self.assertIn("Эталон milestones", body)
        self.assertIn("KR-78", body)
        self.assertIn("https://youtrack.myfin.group/issue/KR-78", body)
        self.assertIn("следующие шаги", body.lower())
        self.assertIn("сторона", body.lower())
        self.assertIn("риск", body.lower())
        self.assertIn("диаграмм", body.lower())
        self.assertIn("гант", body.lower())
        self.assertIn("Данные для диаграммы Ганта", body)
        self.assertIn("| M1 | Модель |", body)
        self.assertNotIn("## Roadmap — куда движется продукт", body)
        self.assertNotIn("docs.google.com", body)
        self.assertIn("Без Google-roadmap", body)

    def test_dispatcher_uses_milestone_prompt(self):
        r = _report(
            generated_text="# Синхронизация с milestone\n\n### M1 — Модель\n- **Следующие шаги:** x",
            raw_data_snapshot={
                "report_format": "milestone_sync",
                "portfolio": True,
                "projects": [
                    {
                        "project_name": "Криптокарта",
                        "report_text": "### M1\n- **Дедлайн:** 28.08.2026",
                        "snapshot": {"milestones": [{"code": "M1", "title": "Модель"}]},
                    }
                ],
            },
        )
        body = build_gamma_prompt_for_report(session=None, report=r)  # type: ignore[arg-type]
        self.assertIn("синхронизация с milestone", body.lower())
        self.assertNotIn("## Roadmap — куда движется продукт", body)

    def test_from_template_uses_corporate_shell(self):
        body = build_gamma_milestone_presentation_prompt(
            report_date="26.08.2026",
            combined_report_text="### M1 — Модель",
            snapshot={"milestones": [{"code": "M1", "title": "Модель"}]},
            from_template=True,
        )
        self.assertIn("корпоративный шаблон", body.lower())
        self.assertIn("диаграмм", body.lower())
        self.assertIn("гант", body.lower())
        self.assertIn("небольш", body.lower())
        self.assertIn("без rag", body.lower())
        self.assertIn("рекомендация, не правило", body.lower())

    def test_num_cards_bumped_for_milestone_default(self):
        r = _report(
            raw_data_snapshot={
                "report_format": "milestone_gamma",
                "projects": [
                    {"snapshot": {"milestones": [{"code": f"M{i}"} for i in range(13)]}}
                ],
            }
        )
        self.assertEqual(gamma_num_cards_for_report(r, 10), 23)
        self.assertEqual(gamma_num_cards_for_report(r, 8), 8)

    def test_gantt_table_uses_previous_deadline_as_start(self):
        snap = {
            "milestones_meta": {"as_of": "2026-08-26"},
            "milestones": [
                {
                    "code": "M1",
                    "title": "Модель",
                    "deadline": "2026-08-28",
                    "days_until_deadline": 2,
                },
                {
                    "code": "M2",
                    "title": "Договор",
                    "deadline": "2026-09-15",
                    "days_until_deadline": 20,
                },
            ],
        }
        block = _milestones_gantt_block(snap)
        self.assertIn("| M1 | Модель | 2026-08-26 | 2026-08-28 | 2 |", block)
        self.assertIn("| M2 | Договор | 2026-08-28 | 2026-09-15 | 20 |", block)


if __name__ == "__main__":
    unittest.main()
