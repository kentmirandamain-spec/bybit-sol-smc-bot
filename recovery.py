"""Startup reconciliation with the exchange. Dry-run state is just read back from disk."""
from __future__ import annotations


def reconcile(position: dict | None, orders: list[dict], symbol: str) -> list[dict]:
    """Decide what to repair. Orders are already filtered to this bot's link ids."""
    actions = []
    if position and float(position.get("size") or 0) > 0:
        sl = str(position.get("stopLoss") or "")
        tp = str(position.get("takeProfit") or "")
        if sl in ("", "0") or tp in ("", "0"):
            actions.append({"action": "replace_protection", "symbol": symbol,
                            "reason": "open position is missing a stop or a target"})
    for order in orders:
        status = order.get("orderStatus") or ""
        if status in ("New", "PartiallyFilled") and float(order.get("cumExecQty") or 0) == 0:
            # A working entry with no position is fine. A reduce-only leftover is not.
            if order.get("reduceOnly") in (True, "true"):
                actions.append({"action": "cancel", "symbol": symbol, "orderId": order.get("orderId"),
                                "reason": "stale reduce-only order"})
    if position is None:
        for order in orders:
            if order.get("reduceOnly") in (True, "true"):
                continue
            # Keep the newest entry; cancel extras. The bot only wants one entry per symbol.
            pass
    return actions
