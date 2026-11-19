"""Learning journal, logistic scorer, and walk-forward guards."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

import learn
from strategy import Setup, run_strategy

ROOT = Path(__file__).resolve().parents[1]
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def _row(ts, session, r, **extra):
    win = r > 0
    base = dict(
        id=f"t{ts}", source="test", taken=True, status="win" if win else "loss",
        created_ts=ts, side=extra.pop("side", "Buy"), r_net=r, tradable=True,
        session=session, hour=3 if session == "Asia" else 15, dow=int(extra.pop("dow", (ts // 86_400_000) % 7)),
        atr_pct=1.0, atr_pctile=50.0, htf_slope=0.2, htf_adx=25.0, htf_swing_atr=1.0,
        sweep_atr=0.4, ob_atr=0.3, fvg_atr=0.1, stop_pct=1.0, liq_r=2.0,
        vol_ratio=1.0, funding=0.0001, loss_streak=0,
    )
    base.update(extra)
    return base


class FeatureTests(unittest.TestCase):
    def test_session_buckets_do_not_overlap(self):
        # 2024-01-01 is a Monday. 00:30 UTC, 09:30 UTC, 15:30 UTC, 22:30 UTC.
        base = 1_704_067_200_000
        self.assertEqual(learn.session_of(base + 30 * 60_000), "Asia")
        self.assertEqual(learn.session_of(base + 9 * 3600_000 + 30 * 60_000), "London")
        self.assertEqual(learn.session_of(base + 15 * 3600_000), "NY")
        self.assertEqual(learn.session_of(base + 22 * 3600_000), "Off")

    def test_attach_records_signal_features_and_excursions(self):
        n = 160
        ts = 1_704_067_200_000 + np.arange(n) * 15 * 60_000
        close = np.linspace(100, 110, n)
        ltf = pd.DataFrame({
            "ts": ts, "open": close, "high": close + 0.8, "low": close - 0.8,
            "close": close, "volume": np.full(n, 12.0),
        })
        htf = ltf.iloc[::4].reset_index(drop=True)
        cfg = json.loads(json.dumps(CFG))
        cfg["ltf_interval"] = 15
        cfg["htf_interval"] = 60
        setup = Setup(
            created_idx=80, created_ts=int(ts[80]), direction=1, entry=float(close[80]),
            stop=float(close[80] - 1), tp=float(close[80] + 2), expiry_idx=92, reason="test",
            status="win", fill_idx=82, exit_idx=90, exit_price=float(close[80] + 2),
            meta={"sweep_level": 100.0, "sweep_extreme": 99.0, "ob_top": 101.0, "ob_bottom": 100.2,
                  "fvg_top": None, "fvg_bottom": None},
        )
        learn.attach([setup], ltf, htf, cfg)
        self.assertEqual(setup.features["session"], learn.session_of(int(ts[80]) + 15 * 60_000))
        self.assertAlmostEqual(setup.features["stop_pct"], 1 / close[80] * 100, places=4)
        self.assertGreater(setup.features["sweep_atr"], 0)
        self.assertEqual(setup.features["fvg_atr"], 0.0)
        self.assertIsNotNone(setup.mfe_r)
        self.assertGreater(setup.mfe_r, 0)
        self.assertEqual(setup.features["loss_streak"], 0)
        gross, net, ok = learn.r_net_of(setup, 0.0002, 0.00055, 5.0, leverage=5)
        self.assertTrue(ok)
        self.assertGreater(net, 0)
        self.assertLess(net, gross)

    def test_prior_loss_streak_does_not_use_the_current_outcome(self):
        n = 80
        ts = 1_704_067_200_000 + np.arange(n) * 15 * 60_000
        close = np.full(n, 100.0)
        ltf = pd.DataFrame({"ts": ts, "open": close, "high": close + 1, "low": close - 1,
                            "close": close, "volume": np.full(n, 1.0)})
        htf = ltf.iloc[::4].reset_index(drop=True)
        cfg = json.loads(json.dumps(CFG))
        a = Setup(created_idx=10, created_ts=int(ts[10]), direction=-1, entry=100, stop=101, tp=98,
                  expiry_idx=20, reason="a", status="loss", fill_idx=11, exit_idx=12, exit_price=101, meta={})
        b = Setup(created_idx=20, created_ts=int(ts[20]), direction=1, entry=100, stop=99, tp=102,
                  expiry_idx=30, reason="b", status="win", fill_idx=21, exit_idx=22, exit_price=102, meta={})
        learn.attach([b, a], ltf, htf, cfg)
        self.assertEqual(a.features["loss_streak"], 0)
        self.assertEqual(b.features["loss_streak"], 1)


class ScorerTests(unittest.TestCase):
    def test_logistic_ranks_winners_higher_and_stays_finite(self):
        rows = []
        ts = 1_700_000_000_000
        for i in range(60):
            win = i % 2 == 0
            rows.append(_row(ts + i * 3_600_000, "NY", 0.8 if win else -1.0, hour=20 if win else 1))
        model = learn.train_model(rows, l2=0.2)
        self.assertIsNotNone(model)
        self.assertTrue(np.all(np.isfinite(model["weights"])))
        high = learn.score_row(model, rows[0])
        low = learn.score_row(model, rows[1])
        self.assertGreater(high, low)
        self.assertGreaterEqual(high, 0)
        self.assertLessEqual(high, 100)

    def test_score_gate_stays_off_until_enough_trades(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = json.loads(json.dumps(CFG))
            cfg["learning"]["enabled"] = True
            cfg["learning"]["min_score"] = 80
            cfg["learning"]["min_trades"] = 100
            model = {"features": learn.MODEL_FEATURES, "median": [0] * len(learn.MODEL_FEATURES),
                     "mean": [0] * len(learn.MODEL_FEATURES), "std": [1] * len(learn.MODEL_FEATURES),
                     "weights": [0] + [0] * len(learn.MODEL_FEATURES), "l2": 1}
            # A zero weight vector scores 50. Gate must not fire below min_trades.
            folder = learn.journal_dir(root)
            folder.mkdir(parents=True)
            (folder / "model.json").write_text(json.dumps(model), encoding="utf-8")
            (folder / "journal.jsonl").write_text("", encoding="utf-8")
            decision = learn.decide(root, cfg, {"session": "NY", "side": "Buy", "hour": 15, "dow": 1,
                                                "atr_pctile": 10, "htf_slope": -1, "stop_pct": 1,
                                                "vol_ratio": 1, "loss_streak": 0})
            self.assertTrue(decision["allowed"])


class WalkForwardTests(unittest.TestCase):
    def _series(self, n_train=70, n_oos=24):
        """Asia always loses 1R, London always wins 0.5R, in both windows."""
        now = 1_750_000_000_000
        oos_start = now - 20 * 86_400_000
        rows = []
        for i in range(n_train):
            ts = oos_start - (n_train - i) * 12 * 3_600_000
            session = "Asia" if i % 2 == 0 else "London"
            rows.append(_row(ts, session, -1.0 if session == "Asia" else 0.5))
        for i in range(n_oos):
            ts = oos_start + i * 12 * 3_600_000
            session = "Asia" if i % 2 == 0 else "London"
            rows.append(_row(ts, session, -1.0 if session == "Asia" else 0.5))
        return rows

    def test_asia_loss_is_proposed_only_because_it_also_loses_out_of_sample(self):
        proposal = learn.walk_forward(self._series(), {"min_bucket": 20, "min_oos_trades": 8, "oos_weeks": 4, "max_filters": 3})
        self.assertTrue(proposal["adopt_filters"], proposal.get("why"))
        self.assertTrue(any(f["feature"] == "session" and f["value"] == "Asia" for f in proposal["filters"]))
        base = proposal["baseline_oos"]["expectancy"]
        filt = proposal["filtered_oos"]["expectancy"]
        self.assertGreater(filt, base)
        self.assertGreater(proposal["filtered_oos"]["win_rate"], proposal["baseline_oos"]["win_rate"])

    def test_no_rule_when_nothing_loses(self):
        now = 1_750_000_000_000
        rows = [_row(now - i * 86_400_000, "London" if i % 2 == 0 else "NY", 0.2) for i in range(120)]
        proposal = learn.walk_forward(rows, {"min_bucket": 20, "min_oos_trades": 8, "oos_weeks": 4})
        self.assertFalse(proposal["adoptable"])
        self.assertEqual(proposal["filters"], [])
        self.assertEqual(proposal["min_score"], 0)

    def test_live_config_never_auto_applies(self):
        self.assertFalse(learn.should_auto_apply({"dry_run": False, "learning": {"auto_apply_dry_run": True}}))
        self.assertFalse(learn.should_auto_apply({"dry_run": True, "learning": {"auto_apply_dry_run": False}}))
        self.assertTrue(learn.should_auto_apply({"dry_run": True, "learning": {"auto_apply_dry_run": True}}))

    def test_apply_backs_up_config_and_reject_does_not(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg_path = root / "config.yaml"
            cfg_path.write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            proposal = {
                "status": "pending", "adoptable": True, "adopt_filters": True, "adopt_score": False,
                "filters": [{"feature": "session", "op": "==", "value": "Asia", "why": "test"}],
                "min_score": 0, "why": "test", "note": "",
            }
            learn.save_proposal(root, proposal)
            applied = learn.apply_proposal(root, cfg_path)
            text = cfg_path.read_text(encoding="utf-8")
            self.assertIn("enabled: true", text)
            self.assertTrue((root / "backups").exists())
            self.assertTrue(list((root / "backups").glob("learning-*")))
            filters = json.loads((learn.journal_dir(root) / "active_filters.json").read_text(encoding="utf-8"))
            self.assertEqual(filters[0]["value"], "Asia")
            self.assertEqual(applied["status"], "applied")
            with self.assertRaises(ValueError):
                learn.reject_proposal(root)


class ReplaySmokeTests(unittest.TestCase):
    def test_run_strategy_attaches_features_without_changing_results(self):
        rng = np.random.default_rng(2)
        n = 500
        steps = rng.normal(0, 0.4, n)
        close = 100 + np.cumsum(steps)
        ts = 1_704_067_200_000 + np.arange(n) * 15 * 60_000
        ltf = pd.DataFrame({
            "ts": ts,
            "open": close,
            "high": close + rng.uniform(0.1, 0.8, n),
            "low": close - rng.uniform(0.1, 0.8, n),
            "close": close,
            "volume": rng.uniform(5, 30, n),
        })
        htf = ltf.iloc[::4].reset_index(drop=True)
        cfg = json.loads(json.dumps(CFG))
        setups = run_strategy(ltf, htf, cfg, 0.01)
        for s in setups:
            self.assertIsInstance(s.features, dict)
            self.assertIn(s.features["session"], {"Asia", "London", "NY", "Off"})
            self.assertIn("stop_pct", s.features)


if __name__ == "__main__":
    unittest.main()
