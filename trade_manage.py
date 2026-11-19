"""Position management shared by the backtest, dry-run, and live bot.

Risk never goes above 5 USDT a trade, and never above the configured risk.
Partial closes are skipped when either piece would be below the exchange minimum.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


MAX_RISK_USDT = 5.0


def parse_symbols(raw, fallback: str = "SOLUSDT") -> list[str]:
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    out = []
    for item in items:
        sym = str(item).strip().upper()
        if sym and sym not in out:
            out.append(sym)
    return out or [fallback]


def capped_risk(risk_usdt: float, streak: int = 0, streak_risk: float = 0.0) -> float:
    """Configured risk, capped at 5 USDT. A losing streak can only lower it."""
    base = min(MAX_RISK_USDT, max(0.0, float(risk_usdt)))
    if streak > 0 and float(streak_risk) > 0:
        return min(base, float(streak_risk))
    return base


def floor_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    return math.floor((qty / step) + 1e-9) * step


def partial_qty(qty: float, pct: float, min_qty: float, qty_step: float) -> float:
    """Size to close at the first target. 0 means the position is too small to split."""
    if pct <= 0 or qty <= 0:
        return 0.0
    part = floor_step(qty * float(pct) / 100.0, qty_step)
    remain = floor_step(qty - part, qty_step)
    if part < min_qty or remain < min_qty:
        return 0.0
    return part


def fee_cushion(entry: float, maker: float, taker: float) -> float:
    return abs(entry) * (abs(maker) + abs(taker))


def breakeven_stop(entry: float, direction: int, maker: float, taker: float) -> float:
    pad = fee_cushion(entry, maker, taker)
    return entry + pad if direction > 0 else entry - pad


def favorable_r(direction: int, entry: float, initial_stop: float, high: float, low: float) -> float:
    risk = abs(entry - initial_stop)
    if risk <= 0:
        return 0.0
    if direction > 0:
        return (high - entry) / risk
    return (entry - low) / risk


def tighten(direction: int, current: float, proposed: float) -> float:
    """Only move the stop in the trade's favor."""
    if direction > 0:
        return max(current, proposed)
    return min(current, proposed)


def trail_stop(direction: int, current: float, high: float, low: float, atr: float, mult: float,
               structure: float | None) -> float:
    if structure is not None:
        return tighten(direction, current, structure)
    if atr <= 0 or mult <= 0:
        return current
    if direction > 0:
        return tighten(direction, current, high - atr * mult)
    return tighten(direction, current, low + atr * mult)


def blended_r(partial_fraction: float, partial_r: float, remainder_r: float) -> float:
    frac = min(1.0, max(0.0, partial_fraction))
    return frac * partial_r + (1.0 - frac) * remainder_r


def loss_streak(trades: list[dict]) -> int:
    streak = 0
    for trade in reversed(trades):
        if trade.get("status") not in ("win", "loss", "closed"):
            continue
        r = trade.get("r")
        pnl = trade.get("pnl_usdt")
        won = (r is not None and float(r) > 0) or (r is None and pnl is not None and float(pnl) > 0)
        if won:
            break
        streak += 1
    return streak


def circuit_blocked(trades: list[dict], max_losses: int, now: datetime | None = None) -> bool:
    """Pause new entries for the rest of the UTC day after N consecutive losses."""
    if max_losses <= 0:
        return False
    now = now or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    todays = []
    for trade in trades:
        closed = str(trade.get("closed_at") or "")
        if closed.startswith(day) and trade.get("status") in ("win", "loss", "closed"):
            todays.append(trade)
    return loss_streak(todays) >= max_losses


def apply_circuit_to_rows(rows: list[dict], max_losses: int) -> list[dict]:
    """Drop trades that a UTC-day circuit breaker would have skipped. Rows need created and r_net."""
    if max_losses <= 0:
        return list(rows)
    kept = []
    streak = 0
    blocked_day = None
    for row in rows:
        day = str(row.get("created") or "")[:10]
        if blocked_day == day:
            continue
        if day != blocked_day:
            if blocked_day is not None and day != blocked_day:
                blocked_day = None
        if blocked_day == day:
            continue
        kept.append(row)
        r = row.get("r_net")
        if r is None:
            continue
        if float(r) > 0:
            streak = 0
            blocked_day = None
        else:
            streak += 1
            if streak >= max_losses:
                blocked_day = day
                streak = 0
    return kept
