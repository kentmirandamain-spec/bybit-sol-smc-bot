import json
import unittest
from datetime import datetime, timezone

import news


class NewsTests(unittest.TestCase):
    def test_high_impact_usd_blocks_and_others_do_not(self):
        payload = [
            {"country": "USD", "impact": "High", "title": "CPI y/y", "date": "2026-11-19 13:30"},
            {"country": "EUR", "impact": "High", "title": "CPI", "date": "2026-11-19 13:30"},
            {"country": "USD", "impact": "Low", "title": "Bill auction", "date": "2026-11-19 15:00"},
        ]
        windows = news.parse_events(payload, 30)
        self.assertEqual(len(windows), 1)
        self.assertIsNotNone(news.in_blackout(datetime(2026, 11, 19, 13, 40, tzinfo=timezone.utc), windows))
        self.assertIsNone(news.in_blackout(datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc), windows))

    def test_unreachable_feed_fails_open(self):
        info = news.upcoming("/tmp", {"news_filter": True, "news_pause_minutes": 15},
                             now=datetime(2026, 11, 19, tzinfo=timezone.utc),
                             fetch=lambda: (_ for _ in ()).throw(OSError("offline")))
        self.assertFalse(info["blocked"])
        self.assertIn("unreachable", info["warning"])

    def test_manual_list_still_blocks_when_the_feed_is_off(self):
        info = news.upcoming("/tmp", {"news_filter": False, "news_pause_minutes": 10,
                                     "news_blackouts": "2026-11-19 13:30"},
                             now=datetime(2026, 11, 19, 13, 32, tzinfo=timezone.utc), fetch=False)
        self.assertTrue(info["blocked"])


if __name__ == "__main__":
    unittest.main()
