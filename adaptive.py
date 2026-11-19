"""Opt-in adaptive timeframe selection. Default behavior is unchanged.

The rules below are pre-registered. They are not searched on the backtest
window. A walk-forward report may still *select* the best fixed pair on the
first 60 days and freeze it for the last 30; that selection is reported
separately from this fixed classifier.

Regimes use only candles that have already closed:
- range (1h ADX < 18, or no 1h trend): do not trade
- high volatility (1h ATR percentile >= 85): do not trade
- strong trend (4h and 1h trends agree, 1h ADX >= 30, ATR percentile < 75):
  4h bias / 1h entry, 2.5R
- ordinary trend (1h ADX >= 22 and ATR percentile between 25 and 80):
  1h bias / 15m entry, 2.0R
- otherwise mixed: 1h bias / 5m entry, 1.5R

At most one position is open. After 3R of realized loss in a UTC day
(15 USDT at 5 USDT risk) no new entry is taken that day.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np
import pandas as pd

from datetime import datetime, timezone

import learn
import smc
from strategy import run_strategy

# (htf minutes, ltf minutes, R target)
PAIRS = (
    (60, 15, 2.0),
    (60, 5, 1.5),
    (240, 60, 2.5),
)
PAIR_NAMES = {
    (60, 15): "1h/15m",
    (60, 5): "1h/5m",
    (240, 60): "4h/1h",
}
DAILY_LOSS_R = 3.0  # 15 USDT / 5 USDT risk


@dataclass(frozen=True)
class Regime:
    name: str
    htf: int | None
    ltf: int | None
    rr: float | None

    @property
    def pair(self):
        if self.htf is None:
            return None
        return (self.htf, self.ltf)


def classify(adx_1h: float, atr_pctile: float, trend_4h: int, trend_1h: int) -> Regime:
    """Map already-closed 1h/4h state to a timeframe pair, or to a skip."""
    if not np.isfinite(adx_1h) or not np.isfinite(atr_pctile):
        return Regime("unknown", None, None, None)
    if atr_pctile >= 85:
        return Regime("high_vol", None, None, None)
    if adx_1h < 18 or int(trend_1h) == 0:
        return Regime("range", None, None, None)
    if int(trend_4h) != 0 and int(trend_4h) == int(trend_1h) and adx_1h >= 30 and atr_pctile < 75:
        return Regime("strong_trend", 240, 60, 2.5)
    if adx_1h >= 22 and 25 <= atr_pctile <= 80:
        return Regime("trend", 60, 15, 2.0)
    return Regime("mixed", 60, 5, 1.5)


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Aggregate a finer candle frame onto clock-aligned buckets. Drops a partial last bucket."""
    if df.empty:
        return df.copy()
    work = df.sort_values("ts").drop_duplicates("ts").copy()
    bucket = (work["ts"].to_numpy(np.int64) // (minutes * 60_000)) * (minutes * 60_000)
    work["bucket"] = bucket
    grouped = work.groupby("bucket", sort=True)
    out = pd.DataFrame({
        "ts": grouped["ts"].min().to_numpy(),
        "open": grouped["open"].first().to_numpy(),
        "high": grouped["high"].max().to_numpy(),
        "low": grouped["low"].min().to_numpy(),
        "close": grouped["close"].last().to_numpy(),
        "volume": grouped["volume"].sum().to_numpy(),
    })
    width = minutes * 60_000
    # A bucket is complete when it contains the expected number of base bars, or at least
    # spans to its last child. Require the last child to start within one base step of the end.
    base = int(work["ts"].diff().median()) if len(work) > 2 else width
    counts = grouped.size().to_numpy()
    expected = max(1, int(round(width / base)))
    out = out[counts >= expected].reset_index(drop=True)
    return out


def regime_table(h1: pd.DataFrame, h4: pd.DataFrame) -> pd.DataFrame:
    """One row per closed 1h bar. ADX, ATR percentile, and 4h trend use no future bars."""
    adx = learn._adx(h1["high"].to_numpy(float), h1["low"].to_numpy(float), h1["close"].to_numpy(float), 14)
    atr = smc.atr(h1, 14)
    pct = learn._rolling_percentile(atr, 100)
    trend4 = smc.analyze(h4, 3, 3, 100).trend
    trend1 = smc.analyze(h1, 3, 3, 100).trend
    h1_close = h1["ts"].to_numpy(np.int64) + 60 * 60_000
    h4_close = h4["ts"].to_numpy(np.int64) + 240 * 60_000
    pos = np.searchsorted(h4_close, h1_close, side="right") - 1
    names, htfs, ltfs, rrs = [], [], [], []
    for i in range(len(h1)):
        t4 = int(trend4[pos[i]]) if pos[i] >= 0 else 0
        reg = classify(float(adx[i]), float(pct[i]), t4, int(trend1[i]))
        names.append(reg.name)
        htfs.append(reg.htf if reg.htf is not None else 0)
        ltfs.append(reg.ltf if reg.ltf is not None else 0)
        rrs.append(reg.rr if reg.rr is not None else 0.0)
    return pd.DataFrame({
        "close_ts": h1_close, "name": names, "htf": htfs, "ltf": ltfs, "rr": rrs,
        "adx": adx, "atr_pctile": pct,
    })


def _utc_day(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d")


def apply_caps(rows: list[dict], max_daily_r: float = DAILY_LOSS_R, max_trades_per_day: int = 6,
               max_consecutive_losses: int = 3) -> list[dict]:
    """One open position, 15 USDT daily loss, 6 fills per UTC day, and a 3-loss circuit.

    Realized R counts on the UTC day the trade closes. The circuit and the daily
    loss both look only at trades that have already exited, which one-position
    guarantees before the next fill. The loss streak resets each UTC day, matching
    the live guard.
    """
    kept = []
    open_until = 0
    day_r: dict[str, float] = {}
    day_n: dict[str, int] = {}
    streak = 0
    streak_day = ""
    blocked_day = None
    ordered = sorted(rows, key=lambda r: (int(r.get("fill_ts") or 0), str(r.get("id") or "")))
    for row in ordered:
        start = int(row.get("fill_ts") or 0)
        exit_ts = int(row.get("exit_ts") or start)
        if start < open_until:
            continue
        fill_day = _utc_day(start)
        if fill_day != streak_day:
            streak_day = fill_day
            streak = 0
            if blocked_day != fill_day:
                blocked_day = None
        if day_r.get(fill_day, 0.0) <= -float(max_daily_r):
            continue
        if max_trades_per_day and day_n.get(fill_day, 0) >= int(max_trades_per_day):
            continue
        if blocked_day == fill_day:
            continue
        kept.append(row)
        day_n[fill_day] = day_n.get(fill_day, 0) + 1
        rnet = float(row["r_net"])
        exit_day = _utc_day(exit_ts)
        day_r[exit_day] = day_r.get(exit_day, 0.0) + rnet
        if exit_day != fill_day:
            streak = 0
            streak_day = exit_day
        if rnet > 0:
            streak = 0
        else:
            streak += 1
            if max_consecutive_losses and streak >= int(max_consecutive_losses):
                blocked_day = exit_day
                streak = 0
        open_until = exit_ts
    return kept


# Kept name so tests and the report can call the combined gate directly.
apply_one_position_and_daily_cap = apply_caps


def _with_pair(cfg: dict, htf_m: int, ltf_m: int, rr: float) -> dict:
    out = copy.deepcopy(cfg)
    out["htf_interval"] = int(htf_m)
    out["ltf_interval"] = int(ltf_m)
    out["adaptive_mode"] = False
    out["strategy"] = dict(out["strategy"])
    out["strategy"]["rr"] = float(rr)
    # This study measures the timeframe choice. Leave breakeven, trail, and partial off
    # so the comparison is not mixed with those optional exits.
    out["strategy"]["breakeven_at_r"] = 0
    out["strategy"]["trail_mode"] = "off"
    out["strategy"]["partial_tp_pct"] = 0
    out["risk"] = dict(out["risk"])
    out["risk"]["risk_usdt"] = 5.0
    out["risk"]["max_daily_loss_usdt"] = 15
    return out


def closed_trades(ltf: pd.DataFrame, htf: pd.DataFrame, cfg: dict, test_start: int) -> list[dict]:
    """Closed, sizeable trades inside the test window, with fill and exit timestamps."""
    ltf_m = int(cfg["ltf_interval"])
    setups = run_strategy(ltf, htf, cfg, 0.01)
    rows = learn.rows_from_setups(setups, cfg, source="backtest")
    by_ts = {}
    for setup in setups:
        by_ts.setdefault(int(setup.created_ts), []).append(setup)
    ts = ltf["ts"].to_numpy(np.int64)
    out = []
    for row in rows:
        if row.get("status") not in ("win", "loss") or row.get("r_net") is None or not row.get("tradable"):
            continue
        matches = by_ts.get(int(row["created_ts"]), [])
        setup = next((s for s in matches if s.status in ("win", "loss") and s.fill_idx is not None), None)
        if setup is None:
            continue
        fill_ts = int(ts[setup.fill_idx])
        if fill_ts < test_start:
            continue
        row = dict(row)
        row["fill_ts"] = fill_ts
        row["exit_ts"] = int(ts[setup.exit_idx]) + ltf_m * 60_000
        row["pair"] = PAIR_NAMES.get((int(cfg["htf_interval"]), ltf_m), f"{cfg['htf_interval']}/{ltf_m}")
        out.append(row)
    return out


def fixed_variant(frames: dict[int, pd.DataFrame], cfg: dict, htf_m: int, ltf_m: int, rr: float,
                  test_start: int) -> list[dict]:
    raw = closed_trades(frames[ltf_m], frames[htf_m], _with_pair(cfg, htf_m, ltf_m, rr), test_start)
    return apply_caps(raw)


def regime_at(table: pd.DataFrame, ts_ms: int) -> Regime:
    """Last regime whose 1h bar had closed at or before ts_ms."""
    if table.empty:
        return Regime("unknown", None, None, None)
    pos = int(np.searchsorted(table["close_ts"].to_numpy(np.int64), int(ts_ms), side="right") - 1)
    if pos < 0:
        return Regime("unknown", None, None, None)
    row = table.iloc[pos]
    if int(row["htf"]) == 0:
        return Regime(str(row["name"]), None, None, None)
    return Regime(str(row["name"]), int(row["htf"]), int(row["ltf"]), float(row["rr"]))


def adaptive_trades(frames: dict[int, pd.DataFrame], cfg: dict, test_start: int,
                    pooled: list[dict] | None = None) -> list[dict]:
    """Take a signal only when its pair is the one the closed 1h regime currently wants.

    `pooled` is the uncapped closed trades from each fixed pair. Pass the rows
    already produced for the fixed-pair comparison so the strategy is not replayed twice.
    """
    table = regime_table(frames[60], frames[240])
    if pooled is None:
        pooled = []
        for htf_m, ltf_m, rr in PAIRS:
            raw = closed_trades(frames[ltf_m], frames[htf_m], _with_pair(cfg, htf_m, ltf_m, rr), test_start)
            pooled.extend(dict(row) for row in raw)
    chosen = []
    for row in pooled:
        # Signal is known at the close of the entry-timeframe bar that created it.
        ltf_m = 5 if row["pair"] == "1h/5m" else (15 if row["pair"] == "1h/15m" else 60)
        signal_ts = int(row["created_ts"]) + ltf_m * 60_000
        reg = regime_at(table, signal_ts)
        if reg.pair is None or reg.pair != _pair_key(row["pair"]) or abs((reg.rr or 0) - _rr_for(row["pair"])) > 1e-9:
            continue
        row = dict(row)
        row["regime"] = reg.name
        chosen.append(row)
    return apply_one_position_and_daily_cap(chosen)


def _pair_key(name: str) -> tuple[int, int]:
    return {v: k for k, v in PAIR_NAMES.items()}[name]


def _rr_for(name: str) -> float:
    key = _pair_key(name)
    for htf_m, ltf_m, rr in PAIRS:
        if (htf_m, ltf_m) == key:
            return rr
    return 0.0


def choose_latest(frames: dict[int, pd.DataFrame]) -> Regime:
    """Regime of the last closed 1h bar. Used by the bot when adaptive_mode is on."""
    table = regime_table(frames[60], frames[240])
    if table.empty:
        return Regime("unknown", None, None, None)
    return regime_at(table, int(table["close_ts"].iloc[-1]))


def summarize_split(rows: list[dict], test_start: int, split_ms: int) -> dict:
    """Full window, first portion (fit), and the rest (out of sample)."""
    def _sub(pred):
        return learn.summarize([r for r in rows if pred(r)])
    return {
        "full": _sub(lambda r: int(r.get("fill_ts") or 0) >= test_start),
        "is": _sub(lambda r: test_start <= int(r.get("fill_ts") or 0) < split_ms),
        "oos": _sub(lambda r: int(r.get("fill_ts") or 0) >= split_ms),
    }


def select_pair_on_fit(variants: dict[str, list[dict]], test_start: int, split_ms: int) -> str | None:
    """Pick the fixed pair with the best in-sample expectancy and at least 8 trades. Frozen for OOS."""
    best_name = None
    best_exp = None
    for name, rows in variants.items():
        stats = summarize_split(rows, test_start, split_ms)["is"]
        if stats["n"] < 8 or stats["expectancy"] is None:
            continue
        if best_exp is None or stats["expectancy"] > best_exp:
            best_exp = stats["expectancy"]
            best_name = name
    return best_name
