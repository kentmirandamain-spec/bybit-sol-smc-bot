"""Opt-in 1h/4h fair-value-gap model. Default trading is unchanged.

`strategy_variant` stays `default`. Nothing here runs unless that flag is
`fvg_mtf`. The knobs below were fixed before the 90-day test. They are not
searched on the holdout.

Rules, and the choices the wording left open:

- An imbalance is a 3-candle fair value gap. Bullish: low[i] > high[i-2],
  zone [high[i-2], low[i]]. Bearish is the mirror. It is known only at the
  close of candle i.
- Unmitigated means no later candle has traded into that zone. A tap is a
  wick into the zone that does not reach the far side. Trading through the
  far side consumes the gap and does not start a setup.
- After a tap, the 5m chart has 24 bars (2 hours) on a 1h gap, or 48 bars
  (4 hours) on a 4h gap, to print a CHoCH in the gap's direction. A CHoCH on
  the tap bar counts. BOS does not. The other timeframe's gaps are not a bias
  filter; a 1h-only run never looks at 4h, and the reverse.
- If the tap bar touches more than one gap, the 4h gap wins, then the gap
  whose near edge is closest to that bar's open. The others are consumed.
- The order block is the last opposite-color 5m candle before the CHoCH
  (close < open for a long), searching at most 20 bars. A doji does not count.
  The limit is the proximal extreme of that whole candle: its high for a long,
  its low for a short. That is the first price a pullback into the candle
  trades. The stop is the distal extreme, pushed farther by the buffer.
- The buffer is either a fixed 0.05 price unit ($0.05 on SOLUSDT) or 0.05%
  of that distal extreme. Both are reported. The pre-registered live/dry
  setting, if the flag is turned on, is 0.05% and a minimum planned 1R.
- The target is the closer of: an unmitigated 1h/4h swing high (long) or
  swing low (short), and the near edge of an opposing unmitigated 1h/4h gap.
  Swings are fractal (3, 3) and only count once their confirmation bar has
  closed. A level already traded on a closed 5m bar is not a target. No
  target, no trade. Planned R below 1 is a separate filter; the study also
  reports the unfiltered set. Passing ``rr_cap`` pulls the target in to that
  many R when the higher-timeframe level is farther. The level is never
  crossed. The bot does not pass a cap, so the flag-on path is unchanged.
- The limit is placed after the CHoCH close and can fill only on a later
  bar. Same-bar stop beats same-bar target. The fill bar cannot take profit.
  Unfilled limits die after 12 bars, if an opposite CHoCH prints, or if price
  reaches the target without trading the entry. One position at a time.
- Risk is forced to 5 USDT. A stop too wide for the minimum quantity is
  skipped. Fees match backtest.py. Slippage is not modelled.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

import risk
import smc
from strategy import Setup

DAILY_LOSS_R = 3.0
PRESET_SOURCES = "both"
PRESET_BUFFER = "pct"
PRESET_MIN_RR = 1.0


@dataclass
class Imbalance:
    tf: str
    direction: int
    top: float
    bottom: float
    confirm_ts: int
    state: str = "open"   # open | hunting | done


@dataclass
class SwingLevel:
    tf: str
    kind: str
    price: float
    confirm_ts: int
    hit_ts: int | None


@dataclass
class ReplayResult:
    setups: list = field(default_factory=list)
    skipped_rr: int = 0
    skipped_no_target: int = 0
    skipped_no_ob: int = 0
    skipped_size: int = 0
    skipped_entry: int = 0


def resample(df: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """Clock-aligned buckets. A bucket that is missing a base bar is dropped."""
    if df.empty:
        return df.copy()
    work = df.sort_values("ts").drop_duplicates("ts").copy()
    width = minutes * 60_000
    bucket = (work["ts"].to_numpy(np.int64) // width) * width
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
    base = int(work["ts"].diff().median()) if len(work) > 2 else width
    expected = max(1, int(round(width / base)))
    counts = grouped.size().to_numpy()
    return out[counts >= expected].reset_index(drop=True)


def find_imbalances(df: pd.DataFrame, tf: str, minutes: int) -> list[Imbalance]:
    """3-candle gaps. Confirmed at the close of the third candle."""
    if len(df) < 3:
        return []
    high = df["high"].to_numpy(float)
    low = df["low"].to_numpy(float)
    ts = df["ts"].to_numpy(np.int64)
    width = minutes * 60_000
    out = []
    for i in range(2, len(df)):
        if low[i] > high[i - 2]:
            out.append(Imbalance(tf, +1, float(low[i]), float(high[i - 2]), int(ts[i]) + width))
        elif high[i] < low[i - 2]:
            out.append(Imbalance(tf, -1, float(low[i - 2]), float(high[i]), int(ts[i]) + width))
    return out


def zone_relation(gap: Imbalance, high: float, low: float) -> str:
    """How this bar meets the gap: through (far side traded), tap, or none."""
    if gap.direction > 0:
        if low <= gap.bottom:
            return "through"
        if low <= gap.top:
            return "tap"
        return "none"
    if high >= gap.top:
        return "through"
    if high >= gap.bottom:
        return "tap"
    return "none"


def swing_levels(df: pd.DataFrame, tf: str, minutes: int, m5_close: np.ndarray,
                 m5_high: np.ndarray, m5_low: np.ndarray) -> list[SwingLevel]:
    """Fractal swings. `hit_ts` is the first later 5m close that traded the level."""
    if df.empty:
        return []
    found = smc.find_swings(df, 3, 3)
    ts = df["ts"].to_numpy(np.int64)
    width = minutes * 60_000
    out = []
    for swing in found:
        confirm = int(ts[swing.confirm_idx]) + width
        born = int(ts[swing.idx]) + width
        start = int(np.searchsorted(m5_close, born, side="right"))
        if swing.kind == "high":
            hits = np.flatnonzero(m5_high[start:] >= swing.price)
        else:
            hits = np.flatnonzero(m5_low[start:] <= swing.price)
        hit = int(m5_close[start + int(hits[0])]) if len(hits) else None
        out.append(SwingLevel(tf, swing.kind, float(swing.price), confirm, hit))
    return out


def last_opposing_candle(direction: int, t: int, open_: np.ndarray, close: np.ndarray,
                         lookback: int) -> int | None:
    """Last opposite-color candle strictly before bar t. Dojis are skipped."""
    stop_at = max(-1, t - 1 - lookback)
    for j in range(t - 1, stop_at, -1):
        if direction > 0 and close[j] < open_[j]:
            return j
        if direction < 0 and close[j] > open_[j]:
            return j
    return None


def buffer_amount(mode: str, extreme: float) -> float:
    if mode == "fixed":
        return 0.05
    if mode == "pct":
        return abs(extreme) * 0.0005
    raise ValueError(f"unknown buffer {mode}")


def capped_target(direction: int, entry: float, stop: float, htf_level: float,
                  rr_cap: float | None) -> float:
    """Closer of the fixed-R price and the higher-timeframe level.

    The higher-timeframe price is a ceiling for a long and a floor for a short.
    A fixed R further away than that level is pulled in. A level further away
    than the fixed R is not used. ``rr_cap`` of None leaves the level unchanged.
    """
    if rr_cap is None:
        return float(htf_level)
    fixed = entry + direction * float(rr_cap) * abs(entry - stop)
    if direction > 0:
        return min(fixed, float(htf_level))
    return max(fixed, float(htf_level))


def nearest_target(direction: int, entry: float, now: int, swings: list[SwingLevel],
                   gaps: list[Imbalance], allowed: set[str]) -> float | None:
    """Closest unmitigated swing or opposing gap edge beyond the entry."""
    prices = []
    for swing in swings:
        if swing.tf not in allowed or swing.confirm_ts > now:
            continue
        if swing.hit_ts is not None and swing.hit_ts <= now:
            continue
        if direction > 0 and swing.kind == "high" and swing.price > entry:
            prices.append(swing.price)
        elif direction < 0 and swing.kind == "low" and swing.price < entry:
            prices.append(swing.price)
    for gap in gaps:
        if gap.tf not in allowed or gap.state != "open" or gap.confirm_ts > now:
            continue
        if gap.direction == direction:
            continue
        near = gap.bottom if direction > 0 else gap.top
        if direction > 0 and near > entry:
            prices.append(near)
        elif direction < 0 and near < entry:
            prices.append(near)
    if not prices:
        return None
    return min(prices) if direction > 0 else max(prices)


def _utc_day(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d")


def apply_caps(rows: list[dict], max_daily_r: float = DAILY_LOSS_R, max_trades_per_day: int = 6,
               max_consecutive_losses: int = 3) -> list[dict]:
    """One open position, 15 USDT daily loss, 6 fills a UTC day, pause after 3 losses."""
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


def _opts(cfg: dict, sources, buffer, min_rr) -> dict:
    raw = dict(cfg.get("fvg_mtf") or {})
    return {
        "sources": sources or raw.get("sources") or PRESET_SOURCES,
        "buffer": buffer or raw.get("buffer") or PRESET_BUFFER,
        "min_rr": PRESET_MIN_RR if min_rr is None else float(min_rr),
        "choch_bars_1h": int(raw.get("choch_bars_1h", 24)),
        "choch_bars_4h": int(raw.get("choch_bars_4h", 48)),
        "ob_lookback": int(raw.get("ob_lookback", 20)),
        "entry_expiry_bars": int(raw.get("entry_expiry_bars", 12)),
        "maker": float((cfg.get("risk") or {}).get("maker_fee", 0.0002)),
        "taker": float((cfg.get("risk") or {}).get("taker_fee", 0.00055)),
        "leverage": float((cfg.get("risk") or {}).get("leverage", 5)),
    }


def _advance(st: Setup, t: int, high: np.ndarray, low: np.ndarray, opposite: bool) -> None:
    d = st.direction
    if st.status == "pending":
        if opposite:
            st.status = "invalidated"
            return
        if t > st.expiry_idx:
            st.status = "expired"
            return
        reached_tp = (d > 0 and high[t] >= st.tp) or (d < 0 and low[t] <= st.tp)
        touched = (d > 0 and low[t] <= st.entry) or (d < 0 and high[t] >= st.entry)
        if reached_tp and not touched:
            st.status = "invalidated"
            return
        if touched:
            st.status = "filled"
            st.fill_idx = t
            st.initial_stop = st.stop
    if st.status != "filled":
        return
    hit_sl = (d > 0 and low[t] <= st.stop) or (d < 0 and high[t] >= st.stop)
    hit_tp = (d > 0 and high[t] >= st.tp) or (d < 0 and low[t] <= st.tp)
    if st.fill_idx == t:
        hit_tp = False
    if hit_sl:
        st.status, st.exit_idx, st.exit_price = "loss", t, st.stop
    elif hit_tp:
        st.status, st.exit_idx, st.exit_price = "win", t, st.tp


def _build_setup(gap: Imbalance, t: int, now: int, direction: int, o, h, l, c, ts,
                 swings, gaps, allowed, opt, result: ReplayResult) -> Setup | None:
    """Return a pending setup, or None. Counts why a CHoCH was rejected."""
    j = last_opposing_candle(direction, t, o, c, opt["ob_lookback"])
    if j is None:
        result.skipped_no_ob += 1
        return None
    if direction > 0:
        entry_raw, extreme = float(h[j]), float(l[j])
        stop_raw = extreme - buffer_amount(opt["buffer"], extreme)
    else:
        entry_raw, extreme = float(l[j]), float(h[j])
        stop_raw = extreme + buffer_amount(opt["buffer"], extreme)
    entry = risk.round_price(entry_raw, 0.01)
    stop = risk.round_stop_away(entry, stop_raw, 0.01)
    close = float(c[t])
    pullback = (direction > 0 and stop < entry < close) or (direction < 0 and stop > entry > close)
    if not pullback or abs(entry - stop) <= 0:
        result.skipped_entry += 1
        return None
    target = nearest_target(direction, entry, now, swings, gaps, allowed)
    if target is None:
        result.skipped_no_target += 1
        return None
    raw_tp = capped_target(direction, entry, stop, target, opt.get("rr_cap"))
    tick = 0.01
    if opt.get("rr_cap") is None:
        tp = risk.round_price(target, tick)
    else:
        # Toward the entry, so a tick cannot step past the higher-timeframe cap.
        steps = raw_tp / tick
        steps = math.floor(steps + 1e-9) if direction > 0 else math.ceil(steps - 1e-9)
        tp = round(steps * tick, 10)
    if (direction > 0 and tp <= entry) or (direction < 0 and tp >= entry):
        result.skipped_no_target += 1
        return None
    planned = abs(tp - entry) / abs(entry - stop)
    if opt["min_rr"] > 0 and planned + 1e-12 < opt["min_rr"]:
        result.skipped_rr += 1
        return None
    sized = risk.position_size(entry, stop, 5.0, risk.InstrumentRules(), opt["leverage"], True,
                               opt["maker"], opt["taker"])
    if not sized.ok:
        result.skipped_size += 1
        return None
    ob_top = float(h[j])
    ob_bottom = float(l[j])
    return Setup(
        created_idx=t, created_ts=int(ts[t]), direction=direction, entry=entry, stop=stop, tp=tp,
        expiry_idx=t + opt["entry_expiry_bars"],
        reason=f"{'bull' if direction > 0 else 'bear'} {gap.tf} FVG tap + 5m CHoCH, "
               f"OB {ob_bottom:.3f}-{ob_top:.3f}, planned {planned:.2f}R",
        meta={
            "ob_top": ob_top, "ob_bottom": ob_bottom,
            "fvg_top": gap.top, "fvg_bottom": gap.bottom,
            "planned_rr": round(planned, 4), "source_tf": gap.tf, "buffer": opt["buffer"],
            "rr_cap": opt.get("rr_cap"), "htf_target": float(target),
            "ob_idx": j,
        },
    )


def replay(m5: pd.DataFrame, h1: pd.DataFrame, h4: pd.DataFrame, cfg: dict, *,
           sources: str | None = None, buffer: str | None = None, min_rr: float | None = None,
           rr_cap: float | None = None) -> ReplayResult:
    """Walk closed 5m bars. Higher-timeframe gaps are visible only after they close.

    ``rr_cap`` is optional. None keeps the original target, the nearest
    higher-timeframe level. A number pulls that target in to this many R and
    never aims past the level.
    """
    opt = _opts(cfg, sources, buffer, min_rr)
    opt["rr_cap"] = None if rr_cap is None else float(rr_cap)
    allowed = {"1h", "4h"} if opt["sources"] == "both" else {opt["sources"]}
    m5 = m5.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    o = m5["open"].to_numpy(float)
    h = m5["high"].to_numpy(float)
    l = m5["low"].to_numpy(float)
    c = m5["close"].to_numpy(float)
    ts = m5["ts"].to_numpy(np.int64)
    close_ts = ts + 5 * 60_000
    choch: dict[int, list[int]] = {}
    for br in smc.analyze(m5, 3, 3, 100).breaks:
        if br.kind == "CHoCH":
            choch.setdefault(br.idx, []).append(br.direction)

    gaps: list[Imbalance] = []
    swings: list[SwingLevel] = []
    if "1h" in allowed:
        gaps += find_imbalances(h1, "1h", 60)
        swings += swing_levels(h1, "1h", 60, close_ts, h, l)
    if "4h" in allowed:
        gaps += find_imbalances(h4, "4h", 240)
        swings += swing_levels(h4, "4h", 240, close_ts, h, l)
    gaps.sort(key=lambda g: (g.confirm_ts, 0 if g.tf == "4h" else 1))

    result = ReplayResult()
    cursor = 0
    hunt: dict | None = None
    active: Setup | None = None

    def try_signal(gap: Imbalance, t: int, now: int) -> str:
        """'setup' if a position was armed, 'reject' if the CHoCH was unusable, else 'wait'."""
        nonlocal active
        dirs = choch.get(t, [])
        if gap.direction not in dirs:
            return "wait"
        setup = _build_setup(gap, t, now, gap.direction, o, h, l, c, ts, swings, gaps, allowed, opt, result)
        gap.state = "done"
        if setup is None:
            return "reject"
        result.setups.append(setup)
        active = setup
        return "setup"

    for t in range(len(m5)):
        now = int(close_ts[t])
        while cursor < len(gaps) and gaps[cursor].confirm_ts <= now:
            cursor += 1
        visible = gaps[:cursor]

        if active is not None and active.created_idx < t:
            opposite = any(d != active.direction for d in choch.get(t, []))
            _advance(active, t, h, l, opposite and active.status == "pending")
            if active.status in ("win", "loss", "expired", "invalidated"):
                active = None

        if hunt is not None:
            gap = hunt["gap"]
            rel = zone_relation(gap, float(h[t]), float(l[t]))
            if rel == "through" or t > hunt["deadline"]:
                gap.state = "done"
                hunt = None
            else:
                outcome = try_signal(gap, t, now)
                if outcome != "wait":
                    hunt = None

        busy = active is not None or hunt is not None
        taps = []
        for gap in visible:
            if gap.state != "open":
                continue
            rel = zone_relation(gap, float(h[t]), float(l[t]))
            if rel == "none":
                continue
            if rel == "through" or busy:
                gap.state = "done"
            else:
                taps.append(gap)
        if busy or not taps:
            continue
        taps.sort(key=lambda g: (0 if g.tf == "4h" else 1, abs((g.top if g.direction > 0 else g.bottom) - float(o[t]))))
        chosen = taps[0]
        for extra in taps[1:]:
            extra.state = "done"
        window = opt["choch_bars_4h"] if chosen.tf == "4h" else opt["choch_bars_1h"]
        outcome = try_signal(chosen, t, now)
        if outcome == "wait":
            chosen.state = "hunting"
            hunt = {"gap": chosen, "deadline": t + window}
    return result


def closed_rows(m5: pd.DataFrame, setups: list, maker: float = 0.0002, taker: float = 0.00055) -> list[dict]:
    """Closed, sizeable trades with the same net R as backtest.py. Risk is 5 USDT."""
    ts = m5["ts"].to_numpy(np.int64)
    rules = risk.InstrumentRules()
    rows = []
    for setup in setups:
        if setup.status not in ("win", "loss") or setup.fill_idx is None or setup.exit_price is None:
            continue
        stop = setup.initial_stop or setup.stop
        sized = risk.position_size(setup.entry, stop, 5.0, rules, 5.0, True, maker, taker)
        if not sized.ok:
            continue
        fees = sized.qty * (setup.entry * maker + setup.exit_price * taker)
        pnl = sized.qty * (setup.exit_price - setup.entry) * setup.direction - fees
        meta = setup.meta or {}
        rows.append({
            "id": f"fvg:{setup.created_ts}:{setup.side}:{meta.get('source_tf')}",
            "fill_ts": int(ts[setup.fill_idx]),
            "exit_ts": int(ts[setup.exit_idx]) + 5 * 60_000,
            "created_ts": int(setup.created_ts),
            "r_net": pnl / 5.0,
            "planned_rr": float(meta.get("planned_rr") or 0.0),
            "source_tf": meta.get("source_tf"),
            "status": setup.status,
            "direction": setup.direction,
            "entry": setup.entry,
            "stop": setup.stop,
            "tp": setup.tp,
            "fill_idx": setup.fill_idx,
            "exit_idx": setup.exit_idx,
            "ob_idx": meta.get("ob_idx"),
            "fvg_top": meta.get("fvg_top"),
            "fvg_bottom": meta.get("fvg_bottom"),
        })
    return rows
