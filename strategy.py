"""
SMC entry model (shared by the live bot and the backtester).

Long setup (short is the mirror image):
  1. HTF bias is bullish (HTF market structure trend = +1).
  2. On the LTF, sell-side liquidity is swept (wick below a prior swing low, close back above).
  3. Within `sweep_max_age` bars after the sweep, LTF prints a bullish CHoCH
     (or BOS if `require_choch` is false).
  4. Place a limit order at the proximal edge of the order block (or FVG) of the
     leg that broke structure. Stop below min(OB low, sweep low) - buffer.
     Take profit at `rr` R, or at opposing liquidity (nearest unswept swing high).
  5. The pending order expires after `entry_expiry_bars` or is invalidated if price
     reaches the TP first / closes beyond the stop / an opposite CHoCH prints.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import List, Optional

import numpy as np
import pandas as pd

import smc
from risk import round_stop_away


@dataclass
class Setup:
    created_idx: int
    created_ts: int          # ms timestamp (open time) of the bar that created the setup
    direction: int           # +1 long, -1 short
    entry: float
    stop: float
    tp: float
    expiry_idx: int
    reason: str
    status: str = "pending"  # pending | filled | expired | invalidated | win | loss
    fill_idx: Optional[int] = None
    exit_idx: Optional[int] = None
    exit_price: Optional[float] = None

    @property
    def side(self) -> str:
        return "Buy" if self.direction > 0 else "Sell"

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)

    def to_dict(self):
        d = asdict(self)
        d["side"] = self.side
        return d


def htf_bias_for_ltf(ltf: pd.DataFrame, htf: pd.DataFrame, htf_trend: np.ndarray,
                     ltf_minutes: int, htf_minutes: int) -> np.ndarray:
    """For every LTF bar, the HTF trend of the last HTF candle closed at/before the LTF close."""
    ltf_close = ltf["ts"].to_numpy(np.int64) + ltf_minutes * 60_000
    htf_close = htf["ts"].to_numpy(np.int64) + htf_minutes * 60_000
    pos = np.searchsorted(htf_close, ltf_close, side="right") - 1
    bias = np.zeros(len(ltf), dtype=int)
    ok = pos >= 0
    bias[ok] = htf_trend[pos[ok]]
    return bias


def _round_tick(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, 10)


def run_strategy(ltf: pd.DataFrame, htf: pd.DataFrame, cfg: dict,
                 tick_size: float = 0.01) -> List[Setup]:
    """
    Replay the strategy bar by bar over closed candles. Returns every setup with
    its simulated outcome. The live bot uses the last one if it is still 'pending'.
    """
    s = cfg["strategy"]
    ltf_min, htf_min = int(cfg["ltf_interval"]), int(cfg["htf_interval"])

    ltf_res = smc.analyze(ltf, s["swing_left"], s["swing_right"], s["sweep_lookback"])
    htf_res = smc.analyze(htf, s["htf_swing_left"], s["htf_swing_right"], s["sweep_lookback"])
    bias = htf_bias_for_ltf(ltf, htf, htf_res.trend, ltf_min, htf_min)

    o = ltf["open"].to_numpy(float)
    h, l, c = ltf["high"].to_numpy(float), ltf["low"].to_numpy(float), ltf["close"].to_numpy(float)
    ts = ltf["ts"].to_numpy(np.int64)
    a = ltf_res.atr
    n = len(ltf)

    breaks_at = {}
    for b in ltf_res.breaks:
        breaks_at.setdefault(b.idx, []).append(b)
    sweeps_at = {}
    for sw in ltf_res.sweeps:
        sweeps_at.setdefault(sw.idx, []).append(sw)
    swing_highs = [x for x in ltf_res.swings if x.kind == "high"]
    swing_lows = [x for x in ltf_res.swings if x.kind == "low"]

    last_sweep = {+1: None, -1: None}
    setups: List[Setup] = []
    active: Optional[Setup] = None    # pending or filled
    warmup = int(s.get("warmup_bars", 50))

    for t in range(n):
        # ---- manage the active setup with bar t's range (setup was created on an earlier bar)
        if active is not None and active.created_idx < t:
            d = active.direction
            if active.status == "pending":
                if t > active.expiry_idx:
                    active.status = "expired"
                elif (d > 0 and h[t] >= active.tp) or (d < 0 and l[t] <= active.tp):
                    if not ((d > 0 and l[t] <= active.entry) or (d < 0 and h[t] >= active.entry)):
                        active.status = "invalidated"   # ran to target without us
                if active.status == "pending":
                    touched = (d > 0 and l[t] <= active.entry) or (d < 0 and h[t] >= active.entry)
                    if touched:
                        active.status = "filled"
                        active.fill_idx = t
            if active.status == "filled":
                hit_sl = (d > 0 and l[t] <= active.stop) or (d < 0 and h[t] >= active.stop)
                hit_tp = (d > 0 and h[t] >= active.tp) or (d < 0 and l[t] <= active.tp)
                if active.fill_idx == t:
                    # Conservative: on the fill candle we can't know the intrabar order,
                    # so only the stop is allowed to trigger.
                    hit_tp = False
                if hit_sl:                       # conservative: SL first if both
                    active.status, active.exit_idx, active.exit_price = "loss", t, active.stop
                elif hit_tp:
                    active.status, active.exit_idx, active.exit_price = "win", t, active.tp
            if active.status in ("expired", "invalidated", "win", "loss"):
                active = None

        # ---- record sweeps
        for sw in sweeps_at.get(t, []):
            last_sweep[sw.direction] = sw

        if t < warmup:
            continue

        # ---- new structure breaks may create a setup
        for br in breaks_at.get(t, []):
            d = br.direction
            # opposite CHoCH invalidates a pending setup
            if active is not None and active.status == "pending" and d != active.direction and br.kind == "CHoCH":
                active.status = "invalidated"
                active = None
            if s["require_choch"] and br.kind != "CHoCH":
                continue
            if s["use_htf_bias"] and bias[t] != d:
                continue
            sw = last_sweep[d]
            if sw is None or t - sw.idx > s["sweep_max_age"] or sw.idx > t:
                continue
            if active is not None:   # one position / order at a time
                if active.status == "filled":
                    continue
                active.status = "expired"   # replaced by fresher setup
                active = None

            ob, fvg = br.order_block, br.fvg
            mode = s["entry_mode"]
            if mode == "fvg":
                if fvg is None:
                    continue
                zone = fvg
            elif mode == "ob_or_fvg" and fvg is not None:
                zone = fvg
            else:
                zone = ob
            buf = s["stop_buffer_atr"] * a[t]
            if d > 0:
                entry = min(zone.top, c[t])
                stop = min(ob.bottom, sw.extreme) - buf
            else:
                entry = max(zone.bottom, c[t])
                stop = max(ob.top, sw.extreme) + buf
            entry = _round_tick(entry, tick_size)
            stop = round_stop_away(entry, stop, tick_size)
            risk = abs(entry - stop)
            if risk <= 0:
                continue
            risk_pct = risk / entry * 100
            if risk_pct < s["min_stop_pct"] or risk_pct > s["max_stop_pct"]:
                continue

            tp = entry + d * s["rr"] * risk
            if s["tp_mode"] == "liquidity":
                pool = swing_highs if d > 0 else swing_lows
                cands = [x.price for x in pool
                         if x.confirm_idx <= t and not x.broken and not x.swept
                         and ((d > 0 and x.price > entry + s["min_rr"] * risk) or
                              (d < 0 and x.price < entry - s["min_rr"] * risk))]
                if cands:
                    tp = min(cands) if d > 0 else max(cands)
                    tp = min(tp, entry + s["max_rr"] * risk) if d > 0 else max(tp, entry - s["max_rr"] * risk)
            tp = _round_tick(tp, tick_size)

            st = Setup(created_idx=t, created_ts=int(ts[t]), direction=d, entry=entry, stop=stop, tp=tp,
                       expiry_idx=t + s["entry_expiry_bars"],
                       reason=f"{'bull' if d > 0 else 'bear'} sweep@{sw.level:.3f} + {br.kind} "
                              f"(HTF bias {bias[t]:+d}), zone {zone.bottom:.3f}-{zone.top:.3f}")
            setups.append(st)
            active = st
            break
    return setups
