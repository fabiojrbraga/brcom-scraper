import unittest
from datetime import datetime, timedelta, timezone

from app.scraper.browser_use_agent import BrowserUseAgent
from app.scraper.instagram_scraper import InstagramScraper


class DirectMessageRulesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = BrowserUseAgent.__new__(BrowserUseAgent)
        cls.scraper = InstagramScraper.__new__(InstagramScraper)

    def test_parse_instagram_timestamp_from_iso(self):
        parsed = self.agent._parse_instagram_timestamp("2026-03-01T12:30:00+00:00")
        self.assertEqual(parsed, datetime(2026, 3, 1, 12, 30, tzinfo=timezone.utc))

    def test_parse_instagram_timestamp_from_relative_days(self):
        now = datetime(2026, 3, 13, 15, 0, tzinfo=timezone.utc)
        parsed = self.agent._parse_instagram_timestamp("2 days ago", now=now)
        self.assertEqual(parsed, now - timedelta(days=2))

    def test_should_send_direct_when_no_history(self):
        should_send, age_days = self.agent._should_send_direct_message(None, 30)
        self.assertTrue(should_send)
        self.assertIsNone(age_days)

    def test_should_send_direct_when_last_message_is_old(self):
        now = datetime(2026, 3, 13, 15, 0, tzinfo=timezone.utc)
        should_send, age_days = self.agent._should_send_direct_message(
            now - timedelta(days=45),
            30,
            now=now,
        )
        self.assertTrue(should_send)
        self.assertAlmostEqual(age_days, 45.0, places=4)

    def test_should_skip_direct_when_last_message_is_recent(self):
        now = datetime(2026, 3, 13, 15, 0, tzinfo=timezone.utc)
        should_send, age_days = self.agent._should_send_direct_message(
            now - timedelta(days=5),
            30,
            now=now,
        )
        self.assertFalse(should_send)
        self.assertAlmostEqual(age_days, 5.0, places=4)

    def test_render_direct_message_replaces_first_name_placeholders(self):
        rendered = self.scraper._render_direct_message(
            "Oi, {{primeiro_nome}}. Tudo bem, {first_name}?",
            "Fabio",
        )
        self.assertEqual(rendered, "Oi, Fabio. Tudo bem, Fabio?")


if __name__ == "__main__":
    unittest.main()
