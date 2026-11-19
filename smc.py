"""
Smart Money Concepts (SMC) indicator logic.

Everything here is computed WITHOUT look-ahead: every event carries the bar
index at which it becomes known (``confirm_idx`` / ``idx``), so the same code
is used by the live bot and the backtester.

Input: a pandas DataFrame with columns open, high, low, close (oldest -> newest),
containing ONLY closed candles.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #
@dataclass
class Swing:
    idx: int            # bar index of the swing candle
    price: float
    kind: str           # "high" | "low"
    confirm_idx: int    # first bar index at whose close the swing is known
    broken: bool = False   # closed through (structure break / liquidity taken)
    swept: bool = False    # wicked through and closed back (liquidity sweep)


@dataclass
class Zone:
    top: float
    bottom: float
    idx: int            # candle index that defines the zone

    def contains(self, price: float) -> bool:
        return self.bottom <= price <= self.top


@dataclass
class StructureBreak:
    idx: int            # bar whose close broke the level
    direction: int      # +1 bullish, -1 bearish
    kind: str           # "BOS" | "CHoCH"
    level: float        # broken swing price
    swing_idx: int
    leg_start: int      # index of the extreme that started the impulse leg
    order_block: Optional[Zone] = None
    fvg: Optional[Zone] = None


@dataclass
class Sweep:
    idx: int            # bar that swept the level
    direction: int      # +1 = sell-side liquidity swept (bullish), -1 = buy-side swept (bearish)
    level: float        # swing price that was swept
    extreme: float      # wick extreme of the sweeping candle
    swing_idx: int


@dataclass
class SMCResult:
    swings: List[Swing] = field(default_factory=list)
    breaks: List[StructureBreak] = field(default_factory=list)
    sweeps: List[Sweep] = field(default_factory=list)
    trend: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))  # trend after each bar close
    atr: np.ndarray = field(default_factory=lambda: np.zeros(0))


# --------------------------------------------------------------------------- #
# Basic helpers
# --------------------------------------------------------------------------- #
def atr(df: pd.DataFrame, period: int = 14) -> np.ndarray:
    h, l, c = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    return pd.Series(tr).ewm(alpha=1.0 / period, adjust=False).mean().to_numpy()


def find_swings(df: pd.DataFrame, left: int = 3, right: int = 3) -> List[Swing]:
    """Fractal swing highs/lows. A swing at i is confirmed at bar i+right."""
    h, l = df["high"].to_numpy(float), df["low"].to_numpy(float)
    n = len(df)
    out: List[Swing] = []
    for i in range(left, n - right):
        lh, rh = h[i - left:i], h[i + 1:i + right + 1]
        if h[i] > lh.max() and h[i] >= rh.max():
            out.append(Swing(i, float(h[i]), "high", i + right))
        ll, rl = l[i - left:i], l[i + 1:i + right + 1]
        if l[i] < ll.min() and l[i] <= rl.min():
            out.append(Swing(i, float(l[i]), "low", i + right))
    out.sort(key=lambda s: (s.confirm_idx, s.idx))
    return out


def find_fvgs(df: pd.DataFrame, start: int, end: int, direction: int) -> List[Zone]:
    """Fair value gaps (3-candle imbalance) whose 3rd candle lies in [start, end]."""
    h, l = df["high"].to_numpy(float), df["low"].to_numpy(float)
    zones = []
    for i in range(max(start, 2), end + 1):
        if direction > 0 and l[i] > h[i - 2]:
            zones.append(Zone(top=float(l[i]), bottom=float(h[i - 2]), idx=i - 1))
        elif direction < 0 and h[i] < l[i - 2]:
            zones.append(Zone(top=float(l[i - 2]), bottom=float(h[i]), idx=i - 1))
    return zones


def _order_block(df: pd.DataFrame, leg_start: int, direction: int, search_back: int = 5) -> Zone:
    """Last opposite-colour candle at/just before the start of the impulse leg."""
    o, c = df["open"].to_numpy(float), df["close"].to_numpy(float)
    h, l = df["high"].to_numpy(float), df["low"].to_numpy(float)
    pick = leg_start
    for j in range(leg_start, max(-1, leg_start - search_back - 1), -1):
        if (direction > 0 and c[j] < o[j]) or (direction < 0 and c[j] > o[j]):
            pick = j
            break
    # Extend the zone to the leg extreme so the stop sits beyond the swing.
    if direction > 0:
        return Zone(top=float(h[pick]), bottom=float(min(l[pick], l[leg_start])), idx=pick)
    return Zone(top=float(max(h[pick], h[leg_start])), bottom=float(l[pick]), idx=pick)


# --------------------------------------------------------------------------- #
# Main analysis pass
# --------------------------------------------------------------------------- #
def analyze(df: pd.DataFrame, swing_left: int = 3, swing_right: int = 3,
            sweep_lookback: int = 100, atr_period: int = 14) -> SMCResult:
    """
    Single forward pass over closed candles producing swings, BOS/CHoCH breaks
    (with order block + FVG of the breaking leg), liquidity sweeps and the
    running trend (+1 bullish, -1 bearish, 0 undefined) after each bar.
    """
    n = len(df)
    h, l, c = df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)
    swings = find_swings(df, swing_left, swing_right)
    res = SMCResult(swings=swings, trend=np.zeros(n, dtype=int), atr=atr(df, atr_period))

    known_highs: List[Swing] = []
    known_lows: List[Swing] = []
    last_high: Optional[Swing] = None   # most recent confirmed, unbroken swing high
    last_low: Optional[Swing] = None
    trend = 0
    si = 0

    for t in range(n):
        # 1) Liquidity sweeps / takes against swings known BEFORE this bar
        for s in known_lows:
            if s.broken or s.swept or t - s.idx > sweep_lookback:
                continue
            if l[t] < s.price:
                if c[t] > s.price:
                    s.swept = True
                    res.sweeps.append(Sweep(t, +1, s.price, float(l[t]), s.idx))
                else:
                    s.broken = True
        for s in known_highs:
            if s.broken or s.swept or t - s.idx > sweep_lookback:
                continue
            if h[t] > s.price:
                if c[t] < s.price:
                    s.swept = True
                    res.sweeps.append(Sweep(t, -1, s.price, float(h[t]), s.idx))
                else:
                    s.broken = True

        # 2) Structure breaks on close
        if last_high is not None and c[t] > last_high.price:
            seg = l[last_high.idx:t + 1]
            leg_start = last_high.idx + int(np.argmin(seg))
            kind = "CHoCH" if trend == -1 else "BOS"
            br = StructureBreak(t, +1, kind, last_high.price, last_high.idx, leg_start)
            br.order_block = _order_block(df, leg_start, +1)
            fvgs = find_fvgs(df, leg_start, t, +1)
            br.fvg = fvgs[0] if fvgs else None
            res.breaks.append(br)
            trend = 1
            last_high.broken = True
            last_high = None
        elif last_low is not None and c[t] < last_low.price:
            seg = h[last_low.idx:t + 1]
            leg_start = last_low.idx + int(np.argmax(seg))
            kind = "CHoCH" if trend == 1 else "BOS"
            br = StructureBreak(t, -1, kind, last_low.price, last_low.idx, leg_start)
            br.order_block = _order_block(df, leg_start, -1)
            fvgs = find_fvgs(df, leg_start, t, -1)
            br.fvg = fvgs[0] if fvgs else None
            res.breaks.append(br)
            trend = -1
            last_low.broken = True
            last_low = None

        # 3) Swings confirmed at this bar's close become usable from next bar
        while si < len(swings) and swings[si].confirm_idx <= t:
            s = swings[si]
            si += 1
            if s.kind == "high":
                known_highs.append(s)
                if not s.broken and (s.idx > (last_high.idx if last_high else -1)):
                    last_high = s
            else:
                known_lows.append(s)
                if not s.broken and (s.idx > (last_low.idx if last_low else -1)):
                    last_low = s
        res.trend[t] = trend

    return res
