"""Daily learning accepts a config only when the held-out window really improved."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import yaml

import learn_daily

ROOT = Path(__file__).resolve().parents[1]


class RuleTests(unittest.TestCase):
    def test_beats_requires_more_net_r_a_modest_drawdown_and_20_trades(self):
        base = {"n": 20, "total_r": 1.0, "max_dd": 5.0}
        ok, _why = learn_daily.beats({"n": 22, "total_r": 1.4, "max_dd": 5.9}, base)
        self.assertTrue(ok)
        ok, why = learn_daily.beats({"n": 22, "total_r": 1.4, "max_dd": 6.1}, base)
        self.assertFalse(ok)
        self.assertIn("drawdown", why)
        ok, why = learn_daily.beats({"n": 19, "total_r": 5.0, "max_dd": 1.0}, base)
        self.assertFalse(ok)
        self.assertIn("20", why)
        ok, why = learn_daily.beats({"n": 25, "total_r": 1.0, "max_dd": 4.0}, base)
        self.assertFalse(ok)
        self.assertIn("net R", why)

    def test_neighbors_change_one_setting_and_pairs_change_two(self):
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        singles = learn_daily.single_neighbors(cfg)
        self.assertGreaterEqual(len(singles), 10)
        for cand in singles:
            self.assertEqual(len(learn_daily.changed_params(cfg, cand)), 1)
        pairs = learn_daily.pair_neighbors(cfg, singles[:6], limit=8)
        self.assertGreater(len(pairs), 0)
        for cand in pairs:
            self.assertEqual(len(learn_daily.changed_params(cfg, cand)), 2)

    def test_winner_ignores_a_three_setting_jump(self):
        base = {"n": 20, "total_r": 0.0, "max_dd": 4.0}
        winner = learn_daily.pick_winner(base, [
            {"params": [{"param": "a"}, {"param": "b"}, {"param": "c"}],
             "metrics": {"n": 30, "total_r": 9.0, "max_dd": 4.0}},
            {"params": [{"param": "rr"}],
             "metrics": {"n": 21, "total_r": 1.0, "max_dd": 4.2}},
        ])
        self.assertEqual(len(winner["params"]), 1)

    def test_cache_appends_and_drops_old_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
            now = 1_800_000_000_000
            calls = []

            def fetch(interval, start, end):
                calls.append((interval, start, end))
                step = interval * 60_000
                ts = list(range(start, end, step))
                return pd.DataFrame({"ts": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0,
                                     "volume": 1.0})

            learn_daily.update_cache(root, cfg, days=2, fetch=fetch, now_ms=now)
            first = pd.read_csv(root / "state" / "learning" / "cache" / "ltf.csv")
            learn_daily.update_cache(root, cfg, days=2, fetch=fetch, now_ms=now + 15 * 60_000)
            second = pd.read_csv(root / "state" / "learning" / "cache" / "ltf.csv")
            self.assertFalse(second["ts"].duplicated().any())
            self.assertGreaterEqual(len(second), len(first))
            self.assertGreaterEqual(len(calls), 4)

    def test_dry_run_applies_and_live_only_writes_a_proposal(self):
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        original = json.loads(json.dumps(cfg))

        def fake_eval(ltf, htf, cand, oos_start, journal):
            names = learn_daily.changed_params(original, cand)
            if names == ["rr"] and float(learn_daily.get_param(cand, "rr")) == 1.5:
                return {"n": 22, "wins": 12, "win_rate": 0.55, "expectancy": 0.14, "total_r": 3.0,
                        "max_dd": 5.5, "equity": [0.5, 1.5, 3.0], "model": None}
            if not names:
                return {"n": 25, "wins": 10, "win_rate": 0.4, "expectancy": 0.04, "total_r": 1.0,
                        "max_dd": 5.0, "equity": [0.2, 1.0], "model": None}
            return {"n": 8, "wins": 2, "win_rate": 0.25, "expectancy": -0.2, "total_r": -1.0,
                    "max_dd": 3.0, "equity": [], "model": None}

        real = learn_daily.evaluate_config
        learn_daily.evaluate_config = fake_eval
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                text = (ROOT / "config.yaml").read_text(encoding="utf-8")
                (root / "config.yaml").write_text(text, encoding="utf-8")
                dry = json.loads(json.dumps(original))
                dry["dry_run"] = True
                blank = pd.DataFrame()
                result = learn_daily.run(root, dry, frames=(blank, blank), now_ms=1_800_000_000_000, pair_limit=0)
                self.assertEqual(result["decision"], "apply", result.get("reason"))
                written = (root / "config.yaml").read_text(encoding="utf-8")
                self.assertRegex(written, r"(?m)^\s*rr:\s*1\.5(\s|#|$)")
                self.assertTrue(list((root / "state" / "learning" / "configs").glob("config_*.yaml")))
                log = (root / "state" / "learning" / "learning_log.jsonl").read_text(encoding="utf-8")
                self.assertIn('"decision": "apply"', log)

            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                text = (ROOT / "config.yaml").read_text(encoding="utf-8")
                (root / "config.yaml").write_text(text, encoding="utf-8")
                live = json.loads(json.dumps(original))
                live["dry_run"] = False
                result = learn_daily.run(root, live, frames=(pd.DataFrame(), pd.DataFrame()), now_ms=1_800_000_000_000, pair_limit=0)
                self.assertEqual(result["decision"], "proposal")
                self.assertEqual((root / "config.yaml").read_text(encoding="utf-8"), text)
                proposal = json.loads((root / "state" / "learning" / "proposal.json").read_text(encoding="utf-8"))
                self.assertEqual(proposal["status"], "pending")
                applied = learn_daily.apply_pending(root)
                self.assertEqual(applied["status"], "applied")
                self.assertRegex((root / "config.yaml").read_text(encoding="utf-8"), r"(?m)^\s*rr:\s*1\.5(\s|#|$)")
                restored = learn_daily.rollback_config(root, applied["previous_config"])
                self.assertRegex((root / "config.yaml").read_text(encoding="utf-8"), r"(?m)^\s*rr:\s*2(\.0)?(\s|#|$)")
                self.assertTrue(restored["restored"].startswith("config_"))
        finally:
            learn_daily.evaluate_config = real


if __name__ == "__main__":
    unittest.main()
