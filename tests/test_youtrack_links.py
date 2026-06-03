import unittest

from app.services.llm.youtrack_links import linkify_youtrack_ids, merge_youtrack_link_rules


class TestYoutrackLinks(unittest.TestCase):
    def test_linkify_plain_id(self):
        snap = {
            "youtrack": [
                {
                    "instance_url": "https://youtrack.example.com",
                    "issues": [{"id": "MOB-42", "summary": "Test"}],
                }
            ]
        }
        text = "- Закрыли MOB-42: оплата"
        out = linkify_youtrack_ids(text, snapshot=snap)
        self.assertIn("[MOB-42](https://youtrack.example.com/issue/MOB-42)", out)

    def test_skips_existing_markdown_link(self):
        snap = {
            "youtrack": [
                {
                    "instance_url": "https://yt.example.com",
                    "issues": [{"id": "MOB-1", "summary": "x"}],
                }
            ]
        }
        text = "См. [MOB-1](https://yt.example.com/issue/MOB-1) и MOB-2"
        out = linkify_youtrack_ids(text, snapshot=snap)
        self.assertEqual(out.count("[MOB-1]("), 1)
        self.assertIn("[MOB-2](https://yt.example.com/issue/MOB-2)", out)

    def test_unknown_prefix_not_linked(self):
        snap = {
            "youtrack": [
                {
                    "instance_url": "https://yt.example.com",
                    "issues": [{"id": "MOB-1", "summary": "x"}],
                }
            ]
        }
        out = linkify_youtrack_ids("OTHER-99", snapshot=snap)
        self.assertEqual(out, "OTHER-99")

    def test_merge_rules_two_projects(self):
        s1 = {
            "youtrack": [
                {"instance_url": "https://a.com", "issues": [{"id": "P1-1"}]}
            ]
        }
        s2 = {
            "youtrack": [
                {"instance_url": "https://b.com", "issues": [{"id": "P2-2"}]}
            ]
        }
        rules = merge_youtrack_link_rules(s1, s2)
        out = linkify_youtrack_ids("P1-1 и P2-2", rules=rules)
        self.assertIn("[P1-1](https://a.com/issue/P1-1)", out)
        self.assertIn("[P2-2](https://b.com/issue/P2-2)", out)


if __name__ == "__main__":
    unittest.main()
