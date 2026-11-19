"""Kline loading: Bybit v5 public API (default) with an OKX public fallback feed."""
from __future__ import annotations

import time
from typing import Optional

import pandas as pd
import requests

COLS = ["ts", "open", "high", "low", "close", "volume"]


def _to_df(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLS)
    df = df.astype({"ts": "int64", "open": float, "high": float, "low": float, "close": float, "volume": float})
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


def drop_unclosed(df: pd.DataFrame, interval_min: int, now_ms: Optional[int] = None) -> pd.DataFrame:
    now_ms = now_ms or int(time.time() * 1000)
    return df[df["ts"] + interval_min * 60_000 <= now_ms].reset_index(drop=True)


# ----------------------------- Bybit ---------------------------------------
def bybit_klines(session, symbol: str, interval_min: int, limit: int = 1000,
                 category: str = "linear", end_ms: Optional[int] = None) -> pd.DataFrame:
    """Latest `limit` candles via a pybit HTTP session (public endpoint, no keys needed)."""
    rows, end = [], end_ms
    while len(rows) < limit:
        kw = dict(category=category, symbol=symbol, interval=str(interval_min), limit=min(1000, limit - len(rows)))
        if end:
            kw["end"] = end
        r = session.get_kline(**kw)
        lst = r["result"]["list"]          # newest first
        if not lst:
            break
        rows += [x[:6] for x in lst]
        end = int(lst[-1][0]) - 1
        if len(lst) < kw["limit"]:
            break
    return drop_unclosed(_to_df(rows), interval_min)


def bybit_klines_range(symbol: str, interval_min: int, start_ms: int, end_ms: int,
                       testnet: bool = False, category: str = "linear") -> pd.DataFrame:
    base = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
    rows, end = [], end_ms
    while end > start_ms:
        r = requests.get(f"{base}/v5/market/kline", timeout=15, params=dict(
            category=category, symbol=symbol, interval=str(interval_min), start=start_ms, end=end, limit=1000))
        r.raise_for_status()
        j = r.json()
        if j.get("retCode") != 0:
            raise RuntimeError(f"Bybit kline error: {j}")
        lst = j["result"]["list"]
        if not lst:
            break
        rows += [x[:6] for x in lst]
        end = int(lst[-1][0]) - 1
        time.sleep(0.1)
    return drop_unclosed(_to_df(rows), interval_min)


# ----------------------------- OKX fallback --------------------------------
_OKX_BAR = {1: "1m", 3: "3m", 5: "5m", 15: "15m", 30: "30m", 60: "1H", 120: "2H", 240: "4H", 360: "6H", 720: "12H", 1440: "1Dutc"}


def okx_inst(symbol: str) -> str:
    return symbol.replace("USDT", "-USDT-SWAP")   # SOLUSDT -> SOL-USDT-SWAP


def okx_klines_range(symbol: str, interval_min: int, start_ms: int, end_ms: int) -> pd.DataFrame:
    rows, after = [], end_ms
    while True:
        r = requests.get("https://www.okx.com/api/v5/market/history-candles", timeout=15, params=dict(
            instId=okx_inst(symbol), bar=_OKX_BAR[interval_min], after=after, limit=100))
        r.raise_for_status()
        j = r.json()
        if j.get("code") != "0":
            raise RuntimeError(f"OKX error: {j}")
        lst = j["data"]
        if not lst:
            break
        rows += [x[:6] for x in lst]
        after = int(lst[-1][0])
        if after <= start_ms:
            break
        time.sleep(0.12)
    df = _to_df(rows)
    return drop_unclosed(df[df["ts"] >= start_ms].reset_index(drop=True), interval_min)


def okx_klines(symbol: str, interval_min: int, limit: int) -> pd.DataFrame:
    end = int(time.time() * 1000)
    start = end - (limit + 2) * interval_min * 60_000
    return okx_klines_range(symbol, interval_min, start, end).tail(limit).reset_index(drop=True)


def bybit_funding_history(symbol: str, start_ms: int, end_ms: int, category: str = "linear"):
    """Public funding rates as (timestamp_ms, rate) oldest first. Empty if Bybit is unreachable."""
    out = []
    end = end_ms
    while end > start_ms and len(out) < 2000:
        r = requests.get("https://api.bybit.com/v5/market/funding/history", timeout=15, params=dict(
            category=category, symbol=symbol, startTime=start_ms, endTime=end, limit=200))
        r.raise_for_status()
        lst = (r.json().get("result") or {}).get("list") or []
        if not lst:
            break
        for item in lst:
            ts = int(item.get("fundingRateTimestamp") or 0)
            if ts:
                out.append((ts, float(item.get("fundingRate") or 0)))
        oldest = min(int(item.get("fundingRateTimestamp") or end) for item in lst)
        if oldest >= end or len(lst) < 200:
            break
        end = oldest - 1
    uniq = {ts: rate for ts, rate in out}
    return sorted(uniq.items())


def okx_last_price(symbol: str) -> float:
    r = requests.get("https://www.okx.com/api/v5/market/ticker", params={"instId": okx_inst(symbol)}, timeout=10)
    r.raise_for_status()
    return float(r.json()["data"][0]["last"])
