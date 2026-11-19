"""Breakeven, trailing, partial size, and the daily loss circuit."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone

import trade_manage


class ManageTests(unittest.TestCase):
    def test_breakeven_covers_fees_and_never_loosens(self):
        be = trade_manage.breakeven_stop(100, 1, 0.0002, 0.00055)
        self.assertGreater(be, 100)
        self.assertAlmostEqual(be - 100, 100 * 0.00075)
        self.assertEqual(trade_manage.tighten(1, 99, 98), 99)
        self.assertEqual(trade_manage.tighten(1, 99, 100.1), 100.1)

    def test_trail_and_partial_respect_the_minimum(self):
        trailed = trade_manage.trail_stop(1, 95, high=110, low=108, atr=2, mult=1, structure=None)
        self.assertAlmostEqual(trailed, 108)
        self.assertEqual(trade_manage.partial_qty(1.0, 50, min_qty=0.1, qty_step=0.1), 0.5)
        self.assertEqual(trade_manage.partial_qty(0.1, 50, min_qty=0.1, qty_step=0.1), 0.0)
        self.assertAlmostEqual(trade_manage.blended_r(0.5, 1.0, 2.0), 1.5)

    def test_risk_never_goes_above_five_or_up_during_a_streak(self):
        self.assertEqual(trade_manage.capped_risk(5), 5)
        self.assertEqual(trade_manage.capped_risk(20), 5)
        self.assertEqual(trade_manage.capped_risk(5, streak=2, streak_risk=2.5), 2.5)
        self.assertEqual(trade_manage.capped_risk(5, streak=2, streak_risk=9), 5)

    def test_three_losses_pause_the_rest_of_the_utc_day(self):
        day = "2026-11-19"
        trades = [{"status": "loss", "r": -1, "closed_at": f"{day}T00:0{i}:00Z"} for i in range(3)]
        now = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
        self.assertTrue(trade_manage.circuit_blocked(trades, 3, now))
        self.assertFalse(trade_manage.circuit_blocked(trades, 3, datetime(2026, 11, 20, tzinfo=timezone.utc)))
        rows = [{"created": f"{day} 0{i}:00 UTC", "r_net": -1} for i in range(3)]
        rows += [{"created": f"{day} 10:00 UTC", "r_net": 2}, {"created": "2026-11-20 01:00 UTC", "r_net": 1}]
        kept = trade_manage.apply_circuit_to_rows(rows, 3)
        self.assertEqual([r["r_net"] for r in kept], [-1, -1, -1, 1])


if __name__ == "__main__":
    unittest.main()
