"""Adaptive timeframe rules. No network."""
from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

import adaptive

ROOT = Path(__file__).resolve().parents[1]
# 2024-01-01 00:00:00 UTC, aligned to 4h.
DAY = 1_704_067_200_000


def _row(i, r, fill_h, exit_h, day0=DAY):
    return {
        "id": f"t{i}",
        "fill_ts": day0 + int(fill_h * 3600_000),
        "exit_ts": day0 + int(exit_h * 3600_000),
        "r_net": r,
        "status": "win" if r > 0 else "loss",
    }


def _candles(n, minutes, start, drift=0.4, vol=0.8):
    ts = start + np.arange(n) * minutes * 60_000
    close = 100 + drift * np.arange(n)
    return pd.DataFrame({
        "ts": ts.astype(np.int64),
        "open": close,
        "high": close + vol,
        "low": close - vol,
        "close": close,
        "volume": np.ones(n),
    })


class ClassifyTests(unittest.TestCase):
    def test_config_defaults_adaptive_off(self):
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        self.assertIs(cfg["adaptive_mode"], False)
        self.assertEqual(cfg["htf_interval"], 60)
        self.assertEqual(cfg["ltf_interval"], 15)
        self.assertIs(cfg["dry_run"], True)
        self.assertEqual(cfg["risk"]["risk_usdt"], 5.0)
        self.assertEqual(cfg["risk"]["max_daily_loss_usdt"], 15)

    def test_low_adx_is_a_skip(self):
        reg = adaptive.classify(12, 40, 1, 1)
        self.assertEqual(reg.name, "range")
        self.assertIsNone(reg.pair)

    def test_flat_structure_is_a_skip(self):
        reg = adaptive.classify(40, 40, 1, 0)
        self.assertEqual(reg.name, "range")
        self.assertIsNone(reg.pair)

    def test_high_vol_skips_before_trend(self):
        reg = adaptive.classify(40, 90, 1, 1)
        self.assertEqual(reg.name, "high_vol")
        self.assertIsNone(reg.pair)

    def test_aligned_strong_trend_uses_4h_1h(self):
        reg = adaptive.classify(32, 50, 1, 1)
        self.assertEqual(reg.name, "strong_trend")
        self.assertEqual(reg.pair, (240, 60))
        self.assertEqual(reg.rr, 2.5)

    def test_ordinary_trend_keeps_the_baseline_pair(self):
        reg = adaptive.classify(24, 50, 0, 1)
        self.assertEqual(reg.name, "trend")
        self.assertEqual(reg.pair, (60, 15))
        self.assertEqual(reg.rr, 2.0)

    def test_mixed_uses_the_faster_pair(self):
        reg = adaptive.classify(20, 10, 1, -1)
        self.assertEqual(reg.name, "mixed")
        self.assertEqual(reg.pair, (60, 5))
        self.assertEqual(reg.rr, 1.5)


class CapTests(unittest.TestCase):
    def test_overlapping_trade_is_dropped(self):
        rows = [_row(1, 1.0, 1, 5), _row(2, 1.0, 3, 8), _row(3, -1.0, 9, 10)]
        kept = adaptive.apply_caps(rows)
        self.assertEqual([r["id"] for r in kept], ["t1", "t3"])

    def test_daily_loss_blocks_the_rest_of_the_utc_day(self):
        rows = [
            _row(1, -1.2, 1, 2),
            _row(2, -1.2, 3, 4),
            _row(3, -1.2, 5, 6),
            _row(4, 2.0, 7, 8),
            _row(5, 1.0, 25, 26),
        ]
        kept = adaptive.apply_caps(rows, max_consecutive_losses=99)
        self.assertEqual([r["id"] for r in kept], ["t1", "t2", "t3", "t5"])

    def test_three_losses_pause_the_rest_of_the_day(self):
        rows = [_row(i, -0.4, 1 + 2 * i, 2 + 2 * i) for i in range(4)]
        rows.append(_row(9, 1.0, 30, 31))
        kept = adaptive.apply_caps(rows)
        self.assertEqual([r["id"] for r in kept], ["t0", "t1", "t2", "t9"])


class LookaheadTests(unittest.TestCase):
    def test_resample_drops_a_partial_bucket(self):
        raw = _candles(7, 5, DAY, drift=0, vol=1)
        out = adaptive.resample(raw, 15)
        self.assertEqual(len(out), 2)
        self.assertEqual(int(out["ts"].iloc[0]), DAY)
        self.assertAlmostEqual(float(out["high"].iloc[0]), float(raw["high"].iloc[:3].max()))
        self.assertAlmostEqual(float(out["close"].iloc[1]), float(raw["close"].iloc[5]))

    def test_earlier_regime_does_not_change_when_future_bars_are_added(self):
        full = _candles(180, 60, DAY, drift=0.35, vol=1.2)
        short = full.iloc[:110].reset_index(drop=True)
        h4_full = adaptive.resample(full, 240)
        h4_short = adaptive.resample(short, 240)
        a = adaptive.regime_table(short, h4_short)
        b = adaptive.regime_table(full, h4_full)
        # Stay clear of the short sample's last swings, which need bars that were cut off.
        self.assertEqual(list(a["name"].iloc[:90]), list(b["name"].iloc[:90]))
        early_close = int(a["close_ts"].iloc[80])
        self.assertEqual(adaptive.regime_at(a, early_close).name, adaptive.regime_at(b, early_close).name)


if __name__ == "__main__":
    unittest.main()
