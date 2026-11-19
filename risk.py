"""Fixed-USDT risk position sizing for Bybit linear contracts."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional


@dataclass
class InstrumentRules:
    qty_step: float = 0.1
    min_qty: float = 0.1
    max_qty: float = 1_000_000.0
    tick_size: float = 0.01
    min_notional: float = 5.0      # Bybit minNotionalValue for linear (USDT)
    max_leverage: float = 100.0


@dataclass
class SizeResult:
    ok: bool
    qty: float = 0.0
    risk_usdt: float = 0.0         # price risk qty*|entry-stop|
    fees_usdt: float = 0.0         # est. entry (maker) + stop exit (taker)
    notional: float = 0.0
    margin: float = 0.0
    reason: str = ""


def _floor_step(x: float, step: float) -> float:
    d, s = Decimal(str(x)), Decimal(str(step))
    return float((d / s).to_integral_value(rounding=ROUND_DOWN) * s)


def round_price(x: float, tick: float) -> float:
    d, s = Decimal(str(x)), Decimal(str(tick))
    return float((d / s).to_integral_value(rounding=ROUND_HALF_UP) * s)


def round_stop_away(entry: float, stop: float, tick: float) -> float:
    """Round the stop to tick AWAY from entry (never shrinks the stop distance)."""
    s = stop / tick
    s = math.floor(s + 1e-9) if stop < entry else math.ceil(s - 1e-9)
    return float(Decimal(str(s)) * Decimal(str(tick)))


def position_size(entry: float, stop: float, risk_usdt: float, rules: InstrumentRules,
                  leverage: float, include_fees: bool = True, maker_fee: float = 0.0002,
                  taker_fee: float = 0.00055, available_usdt: Optional[float] = None) -> SizeResult:
    """qty = risk / |entry - stop| (optionally incl. fees), floored to qty step."""
    dist = abs(entry - stop)
    if dist <= 0 or entry <= 0:
        return SizeResult(False, reason="invalid entry/stop")
    per_unit = dist + ((entry * maker_fee + stop * taker_fee) if include_fees else 0.0)
    raw = risk_usdt / per_unit
    qty = min(_floor_step(raw, rules.qty_step), _floor_step(rules.max_qty, rules.qty_step))
    if qty < rules.min_qty:
        return SizeResult(False, reason=f"size {raw:.4f} < min qty {rules.min_qty} "
                                        f"(stop too wide for {risk_usdt} USDT risk)")
    notional = qty * entry
    if notional < rules.min_notional:
        return SizeResult(False, reason=f"notional {notional:.2f} < min {rules.min_notional}")
    margin = notional / leverage
    if available_usdt is not None and margin * 1.02 > available_usdt:
        return SizeResult(False, reason=f"needs ~{margin:.2f} USDT margin at {leverage}x, "
                                        f"only {available_usdt:.2f} available")
    fees = qty * (entry * maker_fee + stop * taker_fee)
    return SizeResult(True, qty=qty, risk_usdt=qty * dist, fees_usdt=fees,
                      notional=notional, margin=margin)
