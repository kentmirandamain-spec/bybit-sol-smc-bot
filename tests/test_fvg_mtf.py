"""FVG multi-timeframe rules. No network."""
from __future__ import annotations

import math
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

import fvg_mtf

ROOT = Path(__file__).resolve().parents[1]
T0 = 1_704_067_200_000
DAY = T0


def _synthetic():
    """A 1h bullish gap, a 5m CHoCH that wicks into it, and a stop-out on the fill bar."""
    rows = []
    low = 100.0
    start = T0 - 30 * 3600_000
    for i in range(24):
        high = 160.0 if i == 6 else low + 3
        rows.append([start + i * 3600_000, low + 1, high, low, low + 0.5, 1.0])
        low -= 0.4
    rows[-2][1], rows[-2][2], rows[-2][3], rows[-2][4] = 78, 80, 76, 77
    rows.append([start + 24 * 3600_000, 86, 90, 84, 88, 1.0])
    h1 = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    n = 40
    close = np.full(n, 100.0)
    high = close.copy()
    low = close.copy()
    open_ = close.copy()

    def setb(i, o, h, l, c):
        open_[i], high[i], low[i], close[i] = o, h, l, c

    for i in range(0, 6):
        setb(i, 100 + i, 101 + i, 99 + i, 100.5 + i)
    setb(6, 106, 112, 105, 108)
    for i in range(7, 10):
        setb(i, 107, 108, 104, 105)
    for i in range(10, 14):
        setb(i, 104 - (i - 10), 105, 100 - (i - 10), 101)
    setb(14, 100, 101, 90, 92)
    for i in range(15, 18):
        setb(i, 93, 96, 91, 94)
    setb(20, 93, 94, 85, 86)
    for i in range(21, 26):
        setb(i, 86 + (i - 21), 88 + (i - 21), 85, 87 + (i - 21))
    setb(26, 92, 96, 91, 93)
    for i in range(27, 30):
        setb(i, 93, 94, 90, 91)
    setb(30, 100, 100, 81, 100)
    setb(32, 99, 100, 89, 95)
    m5 = pd.DataFrame({
        "ts": T0 + np.arange(n) * 5 * 60_000,
        "open": open_, "high": high, "low": low, "close": close, "volume": np.ones(n),
    })
    return m5, h1


class RuleTests(unittest.TestCase):
    def test_defaults_stay_on_the_original_strategy(self):
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["strategy_variant"], "default")
        self.assertEqual(cfg["htf_interval"], 60)
        self.assertEqual(cfg["ltf_interval"], 15)
        self.assertIs(cfg["dry_run"], True)
        self.assertEqual(cfg["risk"]["risk_usdt"], 5.0)
        self.assertEqual(cfg["risk"]["max_daily_loss_usdt"], 15)
        self.assertEqual(cfg["fvg_mtf"]["buffer"], "pct")
        self.assertEqual(cfg["fvg_mtf"]["min_rr"], 1.0)

    def test_gap_is_known_only_after_the_third_candle(self):
        df = pd.DataFrame({
            "ts": [T0, T0 + 3600_000, T0 + 2 * 3600_000],
            "open": [1, 1, 1], "close": [1, 1, 1], "volume": [1, 1, 1],
            "high": [80, 90, 100], "low": [70, 84, 82],
        })
        gaps = fvg_mtf.find_imbalances(df, "1h", 60)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].direction, 1)
        self.assertEqual(gaps[0].bottom, 80)
        self.assertEqual(gaps[0].top, 82)
        self.assertEqual(gaps[0].confirm_ts, T0 + 3 * 3600_000)

    def test_tap_does_not_count_a_full_fill(self):
        gap = fvg_mtf.Imbalance("1h", 1, top=82, bottom=80, confirm_ts=0)
        self.assertEqual(fvg_mtf.zone_relation(gap, 90, 81), "tap")
        self.assertEqual(fvg_mtf.zone_relation(gap, 90, 80), "through")
        self.assertEqual(fvg_mtf.zone_relation(gap, 90, 85), "none")

    def test_buffers_are_a_fixed_nickel_and_five_basis_points(self):
        self.assertEqual(fvg_mtf.buffer_amount("fixed", 90), 0.05)
        self.assertAlmostEqual(fvg_mtf.buffer_amount("pct", 90), 0.045)

    def test_last_down_candle_is_the_order_block(self):
        o = np.array([10, 10, 9, 8], float)
        c = np.array([11, 9, 9.5, 12], float)
        self.assertEqual(fvg_mtf.last_opposing_candle(+1, 3, o, c, 20), 1)

    def test_future_swing_is_not_a_target(self):
        swings = [
            fvg_mtf.SwingLevel("1h", "high", 110, confirm_ts=1_000, hit_ts=None),
            fvg_mtf.SwingLevel("1h", "high", 101, confirm_ts=5_000, hit_ts=None),
            fvg_mtf.SwingLevel("1h", "high", 105, confirm_ts=500, hit_ts=800),
        ]
        self.assertEqual(fvg_mtf.nearest_target(+1, 100, 2_000, swings, [], {"1h"}), 110)

    def test_min_rr_filter_and_fixed_stop(self):
        m5, h1 = _synthetic()
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        kept = fvg_mtf.replay(m5, h1, h1.iloc[0:0], cfg, sources="1h", buffer="fixed", min_rr=1)
        self.assertEqual(len(kept.setups), 1)
        setup = kept.setups[0]
        self.assertEqual(setup.direction, 1)
        self.assertEqual(setup.entry, 94.0)
        self.assertEqual(setup.stop, 89.95)
        self.assertEqual(setup.meta["ob_idx"], 29)
        self.assertEqual(setup.meta["fvg_bottom"], 80.0)
        self.assertGreater(setup.meta["planned_rr"], 1)
        self.assertEqual(setup.status, "loss")
        skipped = fvg_mtf.replay(m5, h1, h1.iloc[0:0], cfg, sources="1h", buffer="fixed", min_rr=50)
        self.assertEqual(skipped.setups, [])
        self.assertEqual(skipped.skipped_rr, 1)

    def test_full_fill_does_not_arm_a_trade(self):
        m5, h1 = _synthetic()
        m5.loc[30, "low"] = 79
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        res = fvg_mtf.replay(m5, h1, h1.iloc[0:0], cfg, sources="1h", buffer="fixed", min_rr=1)
        self.assertEqual(res.setups, [])

    def test_fixed_r_cannot_aim_past_the_higher_timeframe_level(self):
        # Long: 2R is closer than the swing, so the cap wins. A level inside 2R wins instead.
        self.assertAlmostEqual(fvg_mtf.capped_target(+1, 100, 99, 110, 2), 102)
        self.assertAlmostEqual(fvg_mtf.capped_target(+1, 100, 99, 101.4, 2), 101.4)
        # Short: closer means the higher of the two prices below entry.
        self.assertAlmostEqual(fvg_mtf.capped_target(-1, 100, 101, 90, 2), 98)
        self.assertAlmostEqual(fvg_mtf.capped_target(-1, 100, 101, 99.2, 2), 99.2)
        self.assertEqual(fvg_mtf.capped_target(+1, 100, 99, 110, None), 110)

    def test_replay_cap_pulls_a_far_target_in_to_2r(self):
        m5, h1 = _synthetic()
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        capped = fvg_mtf.replay(m5, h1, h1.iloc[0:0], cfg, sources="1h", buffer="fixed",
                                min_rr=1, rr_cap=2)
        self.assertEqual(len(capped.setups), 1)
        setup = capped.setups[0]
        dist = abs(setup.entry - setup.stop)
        self.assertAlmostEqual(setup.tp, round(math.floor((setup.entry + 2 * dist) / 0.01 + 1e-9) * 0.01, 10))
        self.assertLess(setup.tp, setup.meta["htf_target"])
        self.assertAlmostEqual(setup.meta["planned_rr"], 2.0, places=2)
        tight = fvg_mtf.replay(m5, h1, h1.iloc[0:0], cfg, sources="1h", buffer="fixed",
                               min_rr=1, rr_cap=0.5)
        self.assertEqual(tight.setups, [])
        self.assertEqual(tight.skipped_rr, 1)

    def test_a_later_bar_does_not_rewrite_the_signal(self):
        m5, h1 = _synthetic()
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        first = fvg_mtf.replay(m5, h1, h1.iloc[0:0], cfg, sources="1h", buffer="fixed", min_rr=1).setups[0]
        extra = pd.DataFrame({
            "ts": [int(m5["ts"].iloc[-1]) + 5 * 60_000],
            "open": [50.0], "high": [180.0], "low": [40.0], "close": [45.0], "volume": [1.0],
        })
        longer = pd.concat([m5, extra], ignore_index=True)
        second = fvg_mtf.replay(longer, h1, h1.iloc[0:0], cfg, sources="1h", buffer="fixed", min_rr=1).setups[0]
        self.assertEqual((first.entry, first.stop, first.tp, first.status),
                         (second.entry, second.stop, second.tp, second.status))


class CapTests(unittest.TestCase):
    def _row(self, i, r, fill_h, exit_h):
        return {"id": f"t{i}", "fill_ts": DAY + int(fill_h * 3600_000),
                "exit_ts": DAY + int(exit_h * 3600_000), "r_net": r}

    def test_overlap_and_daily_loss(self):
        rows = [self._row(1, 1, 1, 5), self._row(2, 1, 3, 8), self._row(3, -1, 9, 10)]
        self.assertEqual([r["id"] for r in fvg_mtf.apply_caps(rows)], ["t1", "t3"])
        day = [
            self._row(1, -1.2, 1, 2), self._row(2, -1.2, 3, 4), self._row(3, -1.2, 5, 6),
            self._row(4, 2, 7, 8), self._row(5, 1, 25, 26),
        ]
        kept = fvg_mtf.apply_caps(day, max_consecutive_losses=99)
        self.assertEqual([r["id"] for r in kept], ["t1", "t2", "t3", "t5"])


class ResampleTests(unittest.TestCase):
    def test_partial_bucket_is_dropped(self):
        raw = pd.DataFrame({
            "ts": T0 + np.arange(7) * 5 * 60_000,
            "open": np.arange(7), "high": np.arange(7) + 1,
            "low": np.arange(7) - 1, "close": np.arange(7) + 0.5,
            "volume": np.ones(7),
        })
        out = fvg_mtf.resample(raw, 15)
        self.assertEqual(len(out), 2)
        self.assertEqual(int(out["ts"].iloc[0]), T0)


if __name__ == "__main__":
    unittest.main()
