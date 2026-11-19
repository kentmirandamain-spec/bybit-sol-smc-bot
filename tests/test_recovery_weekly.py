import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import recovery
import trade_manage
import weekly_report


class RecoveryTests(unittest.TestCase):
    def test_missing_protection_is_flagged_and_dry_run_orders_are_not(self):
        actions = recovery.reconcile({"size": "1", "stopLoss": "", "takeProfit": "20"}, [], "SOLUSDT")
        self.assertEqual(actions[0]["action"], "replace_protection")
        self.assertEqual(recovery.reconcile({"size": "1", "stopLoss": "10", "takeProfit": "20"}, [], "SOLUSDT"), [])
        stale = recovery.reconcile(None, [{"orderStatus": "New", "reduceOnly": True, "cumExecQty": "0", "orderId": "1"}], "SOLUSDT")
        self.assertEqual(stale[0]["action"], "cancel")

    def test_symbols_default_to_sol_only(self):
        self.assertEqual(trade_manage.parse_symbols("SOLUSDT"), ["SOLUSDT"])
        self.assertEqual(trade_manage.parse_symbols("solusdt, BTCUSDT, solusdt"), ["SOLUSDT", "BTCUSDT"])
        self.assertNotIn("ETHUSDT", trade_manage.parse_symbols(""))


class WeeklyTests(unittest.TestCase):
    def test_sunday_evening_manila_is_due_once(self):
        # Sunday 20:30 in Manila is 12:30 UTC.
        sunday = datetime(2026, 11, 22, 12, 30, tzinfo=timezone.utc)
        self.assertTrue(weekly_report.due(sunday, None))
        self.assertFalse(weekly_report.due(sunday, weekly_report.week_id(sunday)))
        monday = datetime(2026, 11, 23, 12, 30, tzinfo=timezone.utc)
        self.assertFalse(weekly_report.due(monday, None))

    def test_report_file_uses_r_not_a_bigger_risk(self):
        with tempfile.TemporaryDirectory() as td:
            report = weekly_report.build([
                {"status": "win", "r": 1.2, "closed_at": "2026-11-21T00:00:00+00:00", "symbol": "SOLUSDT", "side": "Buy"},
                {"status": "loss", "r": -1, "closed_at": "2026-11-21T02:00:00+00:00", "symbol": "SOLUSDT", "side": "Sell"},
            ], when=datetime(2026, 11, 22, 12, 30, tzinfo=timezone.utc))
            path = weekly_report.write(Path(td), report)
            text = path.read_text(encoding="utf-8")
            self.assertIn("2", text)
            self.assertIn("5 USDT", text)
            self.assertTrue(path.with_suffix(".json").exists())


if __name__ == "__main__":
    unittest.main()
