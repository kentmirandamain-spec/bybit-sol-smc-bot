#!/usr/bin/env python3
"""
Learning layer for the SOL SMC bot.

It journals every setup, looks for losing conditions, and fits a small
L2-regularized logistic regression in numpy (no scikit-learn). Nothing here
is allowed to change live trading by itself.

Overfit guards, in order:
- Model weights are fit only on the older part of the journal.
- The score cutoff is chosen on an inner slice of that older part, by
  expectancy (average R net of fees), not by win rate.
- A filter is pre-declared (session, side, weekday, a few fixed feature
  cuts). It is kept only if that bucket lost money in the training window
  AND in the most recent held-out weeks, with a minimum sample in both.
- At most three filters are stacked, and only while the held-out window
  still has enough trades, a higher expectancy, and drawdown that is not worse.
- The score gate stays off until the journal has enough closed trades and the
  held-out window says the cutoff helps.
- LIVE / testnet never auto-apply. A proposal waits for Apply in the dashboard.
  Dry-run may apply only when learning.auto_apply_dry_run is true.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

import risk
import smc

ROOT = Path(__file__).resolve().parent
JOURNAL_DIR_NAME = Path("state") / "journal"

MODEL_FEATURES = [
    "hour", "dow", "atr_pct", "atr_pctile", "htf_slope", "htf_adx",
    "htf_swing_atr", "sweep_atr", "ob_atr", "fvg_atr", "stop_pct",
    "liq_r", "vol_ratio", "funding", "loss_streak", "side_long",
    "sess_asia", "sess_london", "sess_ny",
]

# Cuts that are declared before looking at outcomes. Terciles of a few
# continuous features are the only data-dependent edges, and they are taken
# from the training window only.
FIXED_RULES = (
    ("atr_pctile", "<", 25.0, "low ATR percentile"),
    ("atr_pctile", ">", 75.0, "high ATR percentile"),
    ("htf_slope", "<", 0.0, "price drifting against the trade on the higher timeframe"),
    ("vol_ratio", "<", 0.8, "volume below its recent average"),
    ("loss_streak", ">=", 2.0, "two or more losses in a row"),
)


def journal_dir(root: Path) -> Path:
    return Path(root) / "state" / "journal"


def _num(v):
    try:
        if v is None or v == "":
            return None
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def _py(v):
    if isinstance(v, (np.floating, float)):
        x = float(v)
        return None if math.isnan(x) or math.isinf(x) else round(x, 6)
    if isinstance(v, (np.integer, int)) and not isinstance(v, bool):
        return int(v)
    return v


def session_of(ts_ms: int) -> str:
    """Non-overlapping UTC sessions. The signal time is the candle close."""
    hour = datetime.fromtimestamp(int(ts_ms) / 1000, timezone.utc).hour
    if hour < 7:
        return "Asia"
    if hour < 12:
        return "London"
    if hour < 21:
        return "NY"
    return "Off"


def _adx(high, low, close, period: int = 14) -> np.ndarray:
    n = len(close)
    up = np.zeros(n)
    down = np.zeros(n)
    up[1:] = high[1:] - high[:-1]
    down[1:] = low[:-1] - low[1:]
    prev_c = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_c), np.abs(low - prev_c)))
    plus = np.where((up > down) & (up > 0), up, 0.0)
    minus = np.where((down > up) & (down > 0), down, 0.0)

    def wilder(x):
        out = np.zeros(n)
        if n <= period:
            return out
        out[period] = np.sum(x[1:period + 1])
        for i in range(period + 1, n):
            out[i] = out[i - 1] - (out[i - 1] / period) + x[i]
        return out

    atr_s = wilder(tr)
    pdi = 100.0 * wilder(plus) / np.maximum(atr_s, 1e-12)
    mdi = 100.0 * wilder(minus) / np.maximum(atr_s, 1e-12)
    dx = 100.0 * np.abs(pdi - mdi) / np.maximum(pdi + mdi, 1e-12)
    adx = np.zeros(n)
    start = 2 * period
    if n > start:
        adx[start] = float(np.mean(dx[period:start + 1]))
        for i in range(start + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return adx


def _rolling_percentile(x: np.ndarray, window: int = 100) -> np.ndarray:
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        sl = x[max(0, i - window + 1):i + 1]
        sl = sl[np.isfinite(sl)]
        if len(sl) < 5 or not np.isfinite(x[i]):
            continue
        out[i] = 100.0 * float(np.mean(sl <= x[i]))
    return out


def _rolling_mean(x: np.ndarray, window: int = 20) -> np.ndarray:
    out = np.full(len(x), np.nan)
    csum = np.cumsum(np.where(np.isfinite(x), x, 0.0))
    for i in range(window, len(x)):
        out[i] = (csum[i - 1] - (csum[i - window - 1] if i - window - 1 >= 0 else 0.0)) / window
    return out


def attach(setups, ltf: pd.DataFrame, htf: pd.DataFrame, cfg: dict) -> None:
    """Fill features, MFE/MAE and the prior-loss streak. Does not change status."""
    if not setups:
        return
    ltf_min = int(cfg["ltf_interval"])
    htf_min = int(cfg["htf_interval"])
    s_cfg = cfg["strategy"]
    ltf_ts = ltf["ts"].to_numpy(np.int64)
    htf_ts = htf["ts"].to_numpy(np.int64)
    ltf_close_ts = ltf_ts + ltf_min * 60_000
    htf_close_ts = htf_ts + htf_min * 60_000
    htf_pos = np.searchsorted(htf_close_ts, ltf_close_ts, side="right") - 1

    hc = htf["close"].to_numpy(float)
    hh, hl = htf["high"].to_numpy(float), htf["low"].to_numpy(float)
    hatr = smc.atr(htf, 14)
    hadx = _adx(hh, hl, hc, 14)
    slope = np.full(len(htf), np.nan)
    k = 10
    if len(htf) > k:
        slope[k:] = (hc[k:] - hc[:-k]) / (k * np.maximum(hatr[k:], 1e-12))
    hsw = smc.find_swings(htf, int(s_cfg["htf_swing_left"]), int(s_cfg["htf_swing_right"]))
    last_low = np.full(len(htf), np.nan)
    last_high = np.full(len(htf), np.nan)
    lo = hi = np.nan
    si = 0
    for i in range(len(htf)):
        while si < len(hsw) and hsw[si].confirm_idx <= i:
            sw = hsw[si]
            si += 1
            if sw.kind == "low":
                lo = sw.price
            else:
                hi = sw.price
        last_low[i] = lo
        last_high[i] = hi

    lc = ltf["close"].to_numpy(float)
    lh, ll = ltf["high"].to_numpy(float), ltf["low"].to_numpy(float)
    latr = smc.atr(ltf, 14)
    pctile = _rolling_percentile(latr, 100)
    vol = ltf["volume"].to_numpy(float) if "volume" in ltf.columns else np.full(len(ltf), np.nan)
    vol_ma = _rolling_mean(np.nan_to_num(vol, nan=0.0), 20)
    lsw = smc.find_swings(ltf, int(s_cfg["swing_left"]), int(s_cfg["swing_right"]))

    for s in setups:
        t = int(s.created_idx)
        hi_i = int(htf_pos[t]) if 0 <= t < len(htf_pos) else -1
        atr_now = float(latr[t]) if 0 <= t < len(latr) and latr[t] > 0 else float("nan")
        meta = getattr(s, "meta", None) or {}
        risk = s.risk_per_unit
        close_ts = int(ltf_close_ts[t]) if 0 <= t < len(ltf_close_ts) else int(s.created_ts)
        when = datetime.fromtimestamp(close_ts / 1000, timezone.utc)
        direction = int(s.direction)
        htf_slope = float(slope[hi_i]) if hi_i >= 0 and np.isfinite(slope[hi_i]) else None
        if htf_slope is not None:
            htf_slope *= direction   # positive means the higher timeframe drifted with the trade
        swing_px = None
        if hi_i >= 0:
            swing_px = float(last_low[hi_i] if direction > 0 else last_high[hi_i])
            if not np.isfinite(swing_px):
                swing_px = None
        htf_swing_atr = None
        if swing_px is not None and np.isfinite(atr_now) and atr_now > 0 and hi_i >= 0 and np.isfinite(hatr[hi_i]) and hatr[hi_i] > 0:
            htf_swing_atr = abs(float(hc[hi_i]) - swing_px) / float(hatr[hi_i])
        sweep_atr = ob_atr = fvg_atr = None
        if meta and np.isfinite(atr_now) and atr_now > 0:
            if meta.get("sweep_extreme") is not None and meta.get("sweep_level") is not None:
                sweep_atr = abs(float(meta["sweep_extreme"]) - float(meta["sweep_level"])) / atr_now
            if meta.get("ob_top") is not None:
                ob_atr = abs(float(meta["ob_top"]) - float(meta["ob_bottom"])) / atr_now
            if meta.get("fvg_top") is not None:
                fvg_atr = abs(float(meta["fvg_top"]) - float(meta["fvg_bottom"])) / atr_now
            else:
                fvg_atr = 0.0
        liq = _opposing_liquidity(direction, s.entry, t, lsw, lc)
        liq_r = abs(liq - s.entry) / risk if liq is not None and risk > 0 else None
        vol_ratio = None
        if t < len(vol) and np.isfinite(vol[t]) and t < len(vol_ma) and vol_ma[t] > 0:
            vol_ratio = float(vol[t] / vol_ma[t])
        mfe, mae = _excursions(s, lh, ll)
        bars = None
        if s.fill_idx is not None and s.exit_idx is not None:
            bars = int(s.exit_idx) - int(s.fill_idx)
        s.mfe_r = mfe
        s.mae_r = mae
        s.features = {
            "session": session_of(close_ts),
            "hour": when.hour,
            "dow": when.weekday(),
            "atr_pct": (atr_now / float(lc[t]) * 100.0) if t < len(lc) and lc[t] and np.isfinite(atr_now) else None,
            "atr_pctile": float(pctile[t]) if t < len(pctile) and np.isfinite(pctile[t]) else None,
            "htf_slope": htf_slope,
            "htf_adx": float(hadx[hi_i]) if hi_i >= 0 else None,
            "htf_swing_atr": htf_swing_atr,
            "sweep_atr": sweep_atr,
            "ob_atr": ob_atr,
            "fvg_atr": fvg_atr,
            "stop_pct": (risk / s.entry * 100.0) if s.entry else None,
            "liq_r": liq_r,
            "vol_ratio": vol_ratio,
            "funding": None,
            "loss_streak": 0,
            "side": "Buy" if direction > 0 else "Sell",
        }
        s.features = {k: _py(v) if not isinstance(v, str) else v for k, v in s.features.items()}
        if bars is not None:
            s.features["bars"] = bars

    streak = 0
    for s in sorted(setups, key=lambda x: (x.created_idx, x.created_ts)):
        s.features["loss_streak"] = int(streak)
        if s.status == "loss":
            streak += 1
        elif s.status == "win":
            streak = 0


def _opposing_liquidity(direction: int, entry: float, t: int, swings, close: np.ndarray):
    best = None
    for sw in swings:
        if sw.confirm_idx > t:
            continue
        if direction > 0 and sw.kind == "high" and sw.price > entry:
            if t > sw.confirm_idx and np.any(close[sw.confirm_idx:t + 1] > sw.price):
                continue
            best = sw.price if best is None else min(best, sw.price)
        elif direction < 0 and sw.kind == "low" and sw.price < entry:
            if t > sw.confirm_idx and np.any(close[sw.confirm_idx:t + 1] < sw.price):
                continue
            best = sw.price if best is None else max(best, sw.price)
    return best


def _excursions(setup, high, low):
    if setup.fill_idx is None:
        return None, None
    end = setup.exit_idx if setup.exit_idx is not None else setup.fill_idx
    end = min(int(end), len(high) - 1)
    start = int(setup.fill_idx)
    if start > end or setup.risk_per_unit <= 0:
        return None, None
    risk = setup.risk_per_unit
    mfe = mae = 0.0
    for i in range(start, end + 1):
        if setup.direction > 0:
            mfe = max(mfe, (high[i] - setup.entry) / risk)
            mae = max(mae, (setup.entry - low[i]) / risk)
        else:
            mfe = max(mfe, (setup.entry - low[i]) / risk)
            mae = max(mae, (high[i] - setup.entry) / risk)
    return round(mfe, 4), round(mae, 4)


def r_net_of(setup, maker: float, taker: float, risk_usdt: float, rules=None, leverage: float = 5.0,
             include_fees: bool = True):
    """Same net R as backtest.py: position size at the fixed USDT risk, fees taken off."""
    if setup.status not in ("win", "loss") or setup.exit_price is None or setup.risk_per_unit <= 0:
        return None, None, False
    rules = rules or risk.InstrumentRules()
    sz = risk.position_size(setup.entry, setup.stop, float(risk_usdt), rules, float(leverage),
                            bool(include_fees), float(maker), float(taker))
    r_gross = (setup.exit_price - setup.entry) * setup.direction / setup.risk_per_unit
    if not sz.ok:
        return round(r_gross, 4), None, False
    fees = sz.qty * (setup.entry * float(maker) + setup.exit_price * float(taker))
    pnl = sz.qty * (setup.exit_price - setup.entry) * setup.direction - fees
    return round(r_gross, 4), round(pnl / float(risk_usdt), 4), True


def row_from_setup(setup, *, source: str, cfg: dict, taken: bool = True, skip_reason: str = "",
                   score=None, funding=None) -> dict:
    rc = cfg["risk"]
    r_gross, r_net, tradable = r_net_of(
        setup, float(rc["maker_fee"]), float(rc["taker_fee"]), float(rc["risk_usdt"]),
        leverage=float(rc.get("leverage", 5)), include_fees=bool(rc.get("include_fees_in_risk", True)))
    feat = dict(getattr(setup, "features", None) or {})
    if funding is not None and feat.get("funding") is None:
        feat["funding"] = funding
    close_ts = int(setup.created_ts) + int(cfg["ltf_interval"]) * 60_000
    side = "L" if setup.direction > 0 else "S"
    return {
        "id": f"{source}:{setup.created_ts}:{side}",
        "source": source,
        "taken": bool(taken) and setup.status in ("win", "loss", "filled", "pending"),
        "status": "skipped" if skip_reason else setup.status,
        "created_ts": int(setup.created_ts),
        "created": datetime.fromtimestamp(close_ts / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "side": setup.side,
        "entry": setup.entry,
        "stop": setup.stop,
        "tp": setup.tp,
        "r_gross": r_gross,
        "r_net": r_net,
        "tradable": tradable,
        "mfe_r": getattr(setup, "mfe_r", None),
        "mae_r": getattr(setup, "mae_r", None),
        "bars": feat.get("bars"),
        "score": score,
        "skip_reason": skip_reason,
        **{k: feat.get(k) for k in (
            "session", "hour", "dow", "atr_pct", "atr_pctile", "htf_slope", "htf_adx",
            "htf_swing_atr", "sweep_atr", "ob_atr", "fvg_atr", "stop_pct", "liq_r",
            "vol_ratio", "funding", "loss_streak")},
    }


def rows_from_setups(setups, cfg: dict, source: str = "backtest", funding=None) -> list[dict]:
    fund = sorted(funding or [])
    times = [f[0] for f in fund]
    rows = []
    for s in setups:
        rate = None
        if times:
            i = int(np.searchsorted(times, s.created_ts, side="right") - 1)
            if i >= 0:
                rate = fund[i][1]
        # Backtest "taken" means the fill was simulated. Expired/invalidated were not taken.
        taken = s.status in ("win", "loss", "filled", "pending")
        rows.append(row_from_setup(s, source=source, cfg=cfg, taken=taken, funding=rate))
    return rows


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
def load_rows(root: Path) -> list[dict]:
    path = journal_dir(root) / "journal.jsonl"
    if not path.exists():
        return []
    by = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("id"):
            by[row["id"]] = row
    return sorted(by.values(), key=lambda r: (r.get("created_ts") or 0, r["id"]))


def save_rows(root: Path, rows: list[dict]) -> None:
    folder = journal_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows, key=lambda r: (r.get("created_ts") or 0, r.get("id") or ""))
    blob = "".join(json.dumps(r, default=_py) + "\n" for r in ordered)
    tmp = folder / "journal.jsonl.tmp"
    tmp.write_text(blob, encoding="utf-8")
    tmp.replace(folder / "journal.jsonl")
    _write_csv(folder / "journal.csv", ordered)


def merge_rows(root: Path, new_rows: list[dict], replace_source: str | None = None) -> list[dict]:
    old = load_rows(root)
    if replace_source:
        old = [r for r in old if r.get("source") != replace_source]
    by = {r["id"]: r for r in old if r.get("id")}
    for r in new_rows:
        prev = by.get(r["id"])
        if prev and prev.get("skip_reason") and not r.get("skip_reason"):
            r = dict(r)
            r["skip_reason"] = prev["skip_reason"]
            r["status"] = "skipped"
            r["taken"] = False
            r["score"] = prev.get("score", r.get("score"))
        by[r["id"]] = r
    rows = list(by.values())
    save_rows(root, rows)
    return rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    cols = ["id", "source", "taken", "status", "created", "created_ts", "side", "entry", "stop", "tp",
            "r_gross", "r_net", "tradable", "mfe_r", "mae_r", "bars", "session", "hour", "dow",
            "atr_pct", "atr_pctile", "htf_slope", "htf_adx", "htf_swing_atr", "sweep_atr", "ob_atr",
            "fvg_atr", "stop_pct", "liq_r", "vol_ratio", "funding", "loss_streak", "score", "skip_reason"]
    lines = [",".join(cols)]
    for r in rows:
        cells = []
        for c in cols:
            v = r.get(c)
            if v is None:
                cells.append("")
            else:
                text = str(_py(v) if not isinstance(v, str) else v).replace('"', "'")
                if "," in text:
                    text = '"' + text + '"'
                cells.append(text)
        lines.append(",".join(cells))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def closed_tradable(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r.get("status") in ("win", "loss") and r.get("tradable") and _num(r.get("r_net")) is not None:
            out.append(r)
    return out


# --------------------------------------------------------------------------- #
# stats, model, walk-forward
# --------------------------------------------------------------------------- #
def summarize(rows: list[dict]) -> dict:
    rs = [float(r["r_net"]) for r in rows if _num(r.get("r_net")) is not None]
    n = len(rs)
    if n == 0:
        return {"n": 0, "wins": 0, "win_rate": None, "expectancy": None, "total_r": None,
                "max_dd": None, "ci_low": None, "ci_high": None}
    arr = np.asarray(rs, float)
    wins = int(np.sum(arr > 0))
    curve = np.concatenate([[0.0], np.cumsum(arr)])
    peak = np.maximum.accumulate(curve)
    dd = float(np.max(peak - curve))
    lo, hi = _wilson(wins, n)
    return {
        "n": n, "wins": wins, "win_rate": round(wins / n, 4),
        "expectancy": round(float(arr.mean()), 4), "total_r": round(float(arr.sum()), 4),
        "max_dd": round(dd, 4), "ci_low": None if lo is None else round(lo, 4),
        "ci_high": None if hi is None else round(hi, 4),
    }


def _wilson(wins: int, n: int, z: float = 1.96):
    if n <= 0:
        return None, None
    p = wins / n
    z2 = z * z
    den = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / den
    margin = z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n) / den
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _mask(rows: list[dict], feature: str, op: str, value) -> np.ndarray:
    out = np.zeros(len(rows), dtype=bool)
    for i, r in enumerate(rows):
        v = r.get(feature)
        if op == "==":
            out[i] = v == value
            continue
        x = _num(v)
        y = _num(value)
        if x is None or y is None:
            continue
        out[i] = {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}.get(op, False)
    return out


def _apply_filters(rows: list[dict], filters: list[dict]) -> list[dict]:
    if not filters:
        return list(rows)
    keep = []
    for r in rows:
        if any(_mask([r], f["feature"], f["op"], f["value"])[0] for f in filters):
            continue
        keep.append(r)
    return keep


def _bucket_table(rows: list[dict]) -> list[dict]:
    """Descriptive buckets on whatever sample is passed in. Not a trading rule by itself."""
    table = []
    groups = {
        "session": lambda r: r.get("session") or "?",
        "side": lambda r: r.get("side") or "?",
        "dow": lambda r: str(r.get("dow")),
        "atr_pctile": lambda r: _band(_num(r.get("atr_pctile")), [(25, "<25"), (50, "25-50"), (75, "50-75")], ">=75"),
        "htf_slope": lambda r: "with trend" if (_num(r.get("htf_slope")) or 0) >= 0 else "against trend",
        "loss_streak": lambda r: "0" if (_num(r.get("loss_streak")) or 0) <= 0 else ("1" if _num(r.get("loss_streak")) < 2 else ">=2"),
        "vol_ratio": lambda r: _band(_num(r.get("vol_ratio")), [(0.8, "<0.8"), (1.2, "0.8-1.2")], ">=1.2"),
    }
    for feature, fn in groups.items():
        buckets: dict[str, list] = {}
        for r in rows:
            if feature in r and r.get(feature) is None and feature not in ("loss_streak",):
                continue
            buckets.setdefault(str(fn(r)), []).append(r)
        for name, group in buckets.items():
            st = summarize(group)
            st.update(feature=feature, bucket=name,
                      losing=bool(st["n"] >= 20 and st["expectancy"] is not None and st["expectancy"] < 0))
            table.append(st)
    table.sort(key=lambda x: (x["expectancy"] is None, x["expectancy"] if x["expectancy"] is not None else 0))
    return table


def _band(x, edges, last):
    if x is None:
        return "?"
    for edge, label in edges:
        if x < edge:
            return label
    return last


def _candidate_filters(train: list[dict], oos: list[dict], learning: dict) -> list[dict]:
    min_bucket = int(learning.get("min_bucket", 30))
    min_oos_bucket = max(10, min_bucket // 3)
    found = []

    def consider(feature, op, value, label):
        mt = _mask(train, feature, op, value)
        mo = _mask(oos, feature, op, value)
        if int(mt.sum()) < min_bucket or int(mo.sum()) < min_oos_bucket:
            return
        tr = summarize([train[i] for i in range(len(train)) if mt[i]])
        oo = summarize([oos[i] for i in range(len(oos)) if mo[i]])
        base = summarize(train)
        if tr["expectancy"] is None or oo["expectancy"] is None or base["expectancy"] is None:
            return
        # The bucket itself has to be a loser in BOTH windows, and not a tiny dip.
        if tr["expectancy"] > -0.10 or tr["expectancy"] > base["expectancy"] - 0.05:
            return
        if oo["expectancy"] >= 0:
            return
        found.append({
            "feature": feature, "op": op, "value": value if not isinstance(value, str) else value,
            "why": (f"Skip {label}: train {tr['n']} trades, expectancy {tr['expectancy']:+.3f}R, "
                    f"win rate {100 * tr['win_rate']:.0f}% (CI {100 * (tr['ci_low'] or 0):.0f}"
                    f"-{100 * (tr['ci_high'] or 0):.0f}%). Held-out weeks: {oo['n']} trades, "
                    f"expectancy {oo['expectancy']:+.3f}R."),
            "train": tr, "oos": oo,
        })

    for sess in ("Asia", "London", "NY", "Off"):
        consider("session", "==", sess, f"the {sess} session")
    for side in ("Buy", "Sell"):
        consider("side", "==", side, f"{side} setups")
    for dow in range(7):
        consider("dow", "==", dow, f"weekday {dow}")
    for feature, op, value, label in FIXED_RULES:
        consider(feature, op, value, label)
    # One train-quantile cut, pre-declared as "wider than the training 75th percentile stop".
    stops = [x for x in (_num(r.get("stop_pct")) for r in train) if x is not None]
    sweeps = [x for x in (_num(r.get("sweep_atr")) for r in train) if x is not None]
    if len(stops) >= min_bucket:
        q = float(np.quantile(stops, 0.75))
        consider("stop_pct", ">", round(q, 4), f"stop wider than the training 75th percentile ({q:.2f}%)")
    if len(sweeps) >= min_bucket:
        q = float(np.quantile(sweeps, 0.25))
        consider("sweep_atr", "<", round(q, 4), f"sweep shallower than the training 25th percentile ({q:.2f} ATR)")
    found.sort(key=lambda f: f["train"]["expectancy"])
    return found


def _stack_filters(train, oos, candidates, learning: dict) -> list[dict]:
    """Add a candidate only while the held-out window still beats the unfiltered one."""
    min_oos = int(learning.get("min_oos_trades", 20))
    max_n = int(learning.get("max_filters", 3))
    base = summarize(oos)
    chosen = []
    for cand in candidates:
        trial = chosen + [cand]
        kept = _apply_filters(oos, trial)
        st = summarize(kept)
        if st["n"] < min_oos or st["expectancy"] is None or base["expectancy"] is None:
            continue
        if st["expectancy"] <= base["expectancy"]:
            continue
        if st["max_dd"] is not None and base["max_dd"] is not None and st["max_dd"] > base["max_dd"] + 0.5:
            continue
        chosen.append(cand)
        if len(chosen) >= max_n:
            break
    return chosen


def _vector(row: dict) -> list[float]:
    sess = row.get("session")
    side_long = 1.0 if row.get("side") == "Buy" else 0.0
    values = [
        _num(row.get("hour")), _num(row.get("dow")), _num(row.get("atr_pct")), _num(row.get("atr_pctile")),
        _num(row.get("htf_slope")), _num(row.get("htf_adx")), _num(row.get("htf_swing_atr")),
        _num(row.get("sweep_atr")), _num(row.get("ob_atr")), _num(row.get("fvg_atr")),
        _num(row.get("stop_pct")), _num(row.get("liq_r")), _num(row.get("vol_ratio")),
        _num(row.get("funding")), _num(row.get("loss_streak")), side_long,
        1.0 if sess == "Asia" else 0.0, 1.0 if sess == "London" else 0.0, 1.0 if sess == "NY" else 0.0,
    ]
    return [np.nan if v is None else float(v) for v in values]


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, steps: int = 500, lr: float = 0.5) -> np.ndarray:
    """L2-regularized logistic regression. Column 0 is the bias and is not penalized."""
    w = np.zeros(X.shape[1])
    n = max(len(y), 1)
    for _ in range(steps):
        z = np.clip(X @ w, -30, 30)
        p = 1.0 / (1.0 + np.exp(-z))
        grad = X.T @ (p - y) / n
        grad[1:] += (l2 * w[1:]) / n
        w -= lr * grad
    return w


def _design(rows: list[dict], median=None, mean=None, std=None):
    raw = np.asarray([_vector(r) for r in rows], float)
    if median is None:
        median = np.nanmedian(raw, axis=0)
        median = np.where(np.isfinite(median), median, 0.0)
    filled = raw.copy()
    for j in range(filled.shape[1]):
        bad = ~np.isfinite(filled[:, j])
        filled[bad, j] = median[j]
    if mean is None:
        mean = filled.mean(axis=0)
        std = filled.std(axis=0)
        std = np.where(std < 1e-8, 1.0, std)
    norm = (filled - mean) / std
    bias = np.ones((len(rows), 1))
    return np.concatenate([bias, norm], axis=1), median, mean, std


def train_model(train: list[dict], l2: float = 1.0):
    """Fit on the oldest 80% of `train`. Pick the score cutoff on the newest 20%."""
    if len(train) < 40:
        return None
    cut = max(20, int(len(train) * 0.8))
    fit_rows, val_rows = train[:cut], train[cut:]
    y = np.asarray([1.0 if float(r["r_net"]) > 0 else 0.0 for r in fit_rows])
    if y.min() == y.max():
        return None
    X, median, mean, std = _design(fit_rows)
    w = fit_logistic(X, y, l2=l2)
    model = {"features": MODEL_FEATURES, "median": median.tolist(), "mean": mean.tolist(),
             "std": std.tolist(), "weights": w.tolist(), "l2": l2, "fit_n": len(fit_rows)}
    base = summarize(val_rows)
    best = None
    for threshold in (40, 50, 55, 60, 65, 70, 75, 80):
        kept = []
        for r in val_rows:
            sc = score_row(model, r)
            r["_score"] = sc
            if sc >= threshold:
                kept.append(r)
        st = summarize(kept)
        if st["n"] < max(15, int(0.3 * len(val_rows))):
            continue
        if base["expectancy"] is None or st["expectancy"] is None:
            continue
        if st["expectancy"] <= base["expectancy"] + 0.02:
            continue
        if st["max_dd"] is not None and base["max_dd"] is not None and st["max_dd"] > base["max_dd"] + 0.5:
            continue
        if best is None or st["expectancy"] > best[0]:
            best = (st["expectancy"], threshold, st)
    if best is None:
        model["suggested_min_score"] = 0
    else:
        model["suggested_min_score"] = best[1]
        model["inner_val"] = best[2]
    return model


def score_row(model: dict, row: dict) -> float:
    if not model:
        return 0.0
    X, _, _, _ = _design([row], np.asarray(model["median"], float),
                         np.asarray(model["mean"], float), np.asarray(model["std"], float))
    z = float(np.clip(X @ np.asarray(model["weights"], float), -30, 30)[0])
    p = 1.0 / (1.0 + math.exp(-z))
    return round(100.0 * p, 1)


def _with_score(rows: list[dict], model: dict, min_score: float) -> list[dict]:
    kept = []
    for r in rows:
        if score_row(model, r) >= min_score:
            kept.append(r)
    return kept


def walk_forward(rows: list[dict], learning: dict | None = None) -> dict:
    learning = dict(learning or {})
    closed = closed_tradable(rows)
    oos_weeks = int(learning.get("oos_weeks", 4))
    train, oos = _split(closed, oos_weeks)
    base_oos = summarize(oos)
    base_train = summarize(train)
    note = ("Weights and filter cuts use only the older window. The most recent "
            f"{oos_weeks} weeks are a check, not part of the fit. A higher win rate "
            "is reported, but a rule is kept only when held-out expectancy rises and "
            "drawdown does not get worse.")
    proposal = {
        "status": "pending",
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "adoptable": False,
        "adopt_filters": False,
        "adopt_score": False,
        "filters": [],
        "min_score": 0,
        "baseline_oos": base_oos,
        "baseline_train": base_train,
        "filtered_oos": None,
        "model_oos": None,
        "buckets": _bucket_table(closed),
        "train_n": len(train),
        "oos_n": len(oos),
        "oos_weeks": oos_weeks,
        "note": note,
        "why": "Not enough closed trades to learn from yet.",
    }
    min_oos = int(learning.get("min_oos_trades", 20))
    if len(train) < 40 or len(oos) < min_oos:
        proposal["why"] = (f"Need at least 40 training trades and {min_oos} held-out trades "
                           f"(have train={len(train)}, held-out={len(oos)}). No rule was changed.")
        return proposal

    cands = _candidate_filters(train, oos, learning)
    chosen = _stack_filters(train, oos, cands, learning)
    filtered = _apply_filters(oos, chosen) if chosen else oos
    filt_stats = summarize(filtered)
    proposal["filtered_oos"] = filt_stats
    proposal["candidates"] = [{k: c[k] for k in ("feature", "op", "value", "why", "train", "oos")} for c in cands]
    if chosen and filt_stats["expectancy"] is not None and base_oos["expectancy"] is not None \
            and filt_stats["expectancy"] > base_oos["expectancy"] and filt_stats["n"] >= min_oos:
        proposal["adopt_filters"] = True
        proposal["filters"] = [{k: c[k] for k in ("feature", "op", "value", "why")} for c in chosen]

    model = train_model(train, l2=float(learning.get("l2", 1.0)))
    proposal["model"] = _model_public(model) if model else None
    min_score = int(model["suggested_min_score"]) if model else 0
    if model and min_score > 0:
        scored = _with_score(_apply_filters(oos, proposal["filters"]), model, min_score)
        st = summarize(scored)
        proposal["model_oos"] = st
        ref = filt_stats if proposal["adopt_filters"] else base_oos
        if st["n"] >= min_oos and st["expectancy"] is not None and ref["expectancy"] is not None \
                and st["expectancy"] > ref["expectancy"] \
                and (st["max_dd"] is None or ref["max_dd"] is None or st["max_dd"] <= ref["max_dd"] + 0.5):
            proposal["adopt_score"] = True
            proposal["min_score"] = min_score
        else:
            proposal["min_score"] = 0
    proposal["adoptable"] = bool(proposal["adopt_filters"] or proposal["adopt_score"])
    if proposal["adoptable"]:
        proposal["why"] = _proposal_why(proposal)
    else:
        proposal["why"] = ("No filter or score cutoff beat the current rules on the held-out weeks "
                           "without deepening the drawdown. Nothing was changed.")
        proposal["filters"] = []
        proposal["min_score"] = 0
    return proposal


def _model_public(model: dict | None):
    if not model:
        return None
    return {k: model[k] for k in ("features", "median", "mean", "std", "weights", "l2", "fit_n", "suggested_min_score")
            if k in model}


def _proposal_why(p: dict) -> str:
    b, f = p["baseline_oos"], p.get("filtered_oos") or {}
    parts = [f"Held-out baseline: {b['n']} trades, win rate {_pct(b.get('win_rate'))}, "
             f"expectancy {_r(b.get('expectancy'))}, max DD {_dd(b.get('max_dd'))}."]
    if p.get("adopt_filters"):
        parts.append(f"After filters: {f.get('n')} trades, win rate {_pct(f.get('win_rate'))}, "
                     f"expectancy {_r(f.get('expectancy'))}, max DD {_dd(f.get('max_dd'))}.")
    if p.get("adopt_score"):
        m = p.get("model_oos") or {}
        parts.append(f"With score >= {p['min_score']}: {m.get('n')} trades, win rate {_pct(m.get('win_rate'))}, "
                     f"expectancy {_r(m.get('expectancy'))}, max DD {_dd(m.get('max_dd'))}.")
    return " ".join(parts)


def _pct(x):
    return "n/a" if x is None else f"{100 * x:.1f}%"


def _r(x):
    return "n/a" if x is None else f"{x:+.3f}R" if abs(x) < 50 else f"{x:.2f}R"


def _dd(x):
    return "n/a" if x is None else f"{x:.3f}R"


def _split(rows: list[dict], oos_weeks: int):
    rows = sorted(rows, key=lambda r: r.get("created_ts") or 0)
    if len(rows) < 10:
        return rows, []
    last = rows[-1]["created_ts"]
    cut = last - oos_weeks * 7 * 24 * 3600 * 1000
    train = [r for r in rows if (r.get("created_ts") or 0) < cut]
    oos = [r for r in rows if (r.get("created_ts") or 0) >= cut]
    if len(train) < 40 or len(oos) < 10:
        k = max(1, len(rows) // 4)
        return rows[:-k], rows[-k:]
    return train, oos


# --------------------------------------------------------------------------- #
# decisions
# --------------------------------------------------------------------------- #
def load_filters(root: Path) -> list[dict]:
    path = journal_dir(root) / "active_filters.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def load_model(root: Path) -> dict | None:
    path = journal_dir(root) / "model.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("weights") else None


def count_closed(root: Path) -> int:
    return len(closed_tradable(load_rows(root)))


def decide(root: Path, cfg: dict, features: dict) -> dict:
    """Return allowed/reason/score. Disabled learning allows every setup."""
    lc = cfg.get("learning") or {}
    features = features or {}
    model = load_model(root)
    score = score_row(model, _score_features(features)) if model else None
    skip = str(lc.get("session_skip") or "").strip()
    if skip and features.get("session") == skip:
        return {"allowed": False, "reason": f"session filter skips {skip}", "score": score}
    if not lc.get("enabled"):
        return {"allowed": True, "reason": "", "score": score}
    for f in load_filters(root):
        if _mask([features], f["feature"], f["op"], f["value"])[0]:
            return {"allowed": False, "reason": f.get("why") or f"filter {f['feature']} {f['op']} {f['value']}",
                    "score": score}
    min_score = float(lc.get("min_score") or 0)
    closed = count_closed(root)
    need = int(lc.get("min_trades", 100))
    score_ready = bool(lc.get("score_active")) or closed >= need
    if model and min_score > 0 and score_ready and score is not None and score < min_score:
        return {"allowed": False, "reason": f"score {score:.0f} is below {min_score:.0f}", "score": score}
    return {"allowed": True, "reason": "", "score": score}


def _score_features(features: dict) -> dict:
    """decide() is given the feature dict, which uses the same keys as a journal row."""
    row = dict(features)
    if "side" not in row and features.get("side_long") is not None:
        row["side"] = "Buy" if features.get("side_long") else "Sell"
    return row


def should_auto_apply(cfg: dict) -> bool:
    """Dry-run only, and only when the config flag is on. Never for live or testnet orders."""
    lc = cfg.get("learning") or {}
    return bool(lc.get("auto_apply_dry_run")) and cfg.get("dry_run") is True


def retrain_due(root: Path, days: int = 7) -> bool:
    path = journal_dir(root) / "last_study.txt"
    try:
        ts = float(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return True
    return time.time() - ts > days * 86400


def save_proposal(root: Path, proposal: dict) -> None:
    folder = journal_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "proposal.json").write_text(json.dumps(proposal, indent=2, default=_py) + "\n", encoding="utf-8")
    (folder / "last_study.txt").write_text(str(time.time()), encoding="utf-8")
    if proposal.get("model"):
        (folder / "model.json").write_text(json.dumps(proposal["model"], default=_py) + "\n", encoding="utf-8")


def load_proposal(root: Path) -> dict | None:
    path = journal_dir(root) / "proposal.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def apply_proposal(root: Path, cfg_path: Path | None = None) -> dict:
    proposal = load_proposal(root)
    if not proposal or proposal.get("status") != "pending":
        raise ValueError("There is no pending learning proposal.")
    if not proposal.get("adoptable"):
        raise ValueError("This study did not beat the current rules out of sample, so there is nothing to apply.")
    cfg_path = cfg_path or (Path(root) / "config.yaml")
    text = cfg_path.read_text(encoding="utf-8")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path(root) / "backups" / f"learning-{stamp}"
    backup.mkdir(parents=True, exist_ok=True)
    (backup / "config.yaml").write_text(text, encoding="utf-8")
    cfg_path.with_suffix(".yaml.bak").write_text(text, encoding="utf-8")
    updated = _set_learning_keys(text, enabled=True, min_score=int(proposal.get("min_score") or 0))
    cfg_path.write_text(updated, encoding="utf-8")
    filters = proposal["filters"] if proposal.get("adopt_filters") else []
    (journal_dir(root) / "active_filters.json").write_text(json.dumps(filters, indent=2) + "\n", encoding="utf-8")
    proposal["status"] = "applied"
    proposal["applied_at"] = stamp
    proposal["backup"] = str(backup)
    save_proposal(root, proposal)
    return proposal


def reject_proposal(root: Path) -> dict:
    proposal = load_proposal(root)
    if not proposal or proposal.get("status") != "pending":
        raise ValueError("There is no pending learning proposal.")
    proposal["status"] = "rejected"
    save_proposal(root, proposal)
    return proposal


def _set_learning_keys(text: str, enabled: bool, min_score: int) -> str:
    def repl(key, value):
        nonlocal text
        import re
        pat = re.compile(rf"^(\s*{key}:\s*)([^#\n]*?)(\s*#.*)?$", re.M)
        if not pat.search(text):
            raise ValueError(f"config.yaml has no learning.{key} line to update")
        text = pat.sub(lambda m: f"{m.group(1)}{value}{m.group(3) or ''}", text, count=1)
    repl("enabled", "true" if enabled else "false")
    repl("min_score", str(int(min_score)))
    return text


# --------------------------------------------------------------------------- #
# study runner (download + journal + proposal)
# --------------------------------------------------------------------------- #
def run_study(root: Path, cfg: dict, days: int = 90, source: str = "bybit", on_progress=None) -> str:
    def progress(msg: str):
        if on_progress:
            on_progress(msg)

    progress(f"Downloading {days} days of {source} candles…")
    ltf, htf, used = _download(cfg, days, source)
    progress(f"Data: {used}, {len(ltf)} entry candles, {len(htf)} bias candles. Replaying the strategy…")
    from strategy import run_strategy
    rules = risk.InstrumentRules()
    setups = run_strategy(ltf, htf, cfg, rules.tick_size)
    funding = _funding(cfg["symbol"], int(ltf["ts"].iloc[0]), int(ltf["ts"].iloc[-1]) + int(cfg["ltf_interval"]) * 60_000)
    rows = rows_from_setups(setups, cfg, source="backtest", funding=funding)
    merge_rows(root, rows, replace_source="backtest")
    progress(f"Journaled {len(rows)} backtest setups. Walk-forward on the closed trades…")
    proposal = walk_forward(load_rows(root), cfg.get("learning") or {})
    proposal["data_source"] = used
    proposal["days"] = days
    if should_auto_apply(cfg) and proposal.get("adoptable"):
        save_proposal(root, proposal)
        apply_proposal(root, Path(root) / "config.yaml")
        proposal = load_proposal(root)
        progress("Dry-run auto-apply is on. The proposal was written into config.yaml (backup kept).")
    else:
        save_proposal(root, proposal)
        if cfg.get("dry_run") is not True and (cfg.get("learning") or {}).get("auto_apply_dry_run"):
            progress("auto_apply_dry_run is on, but this config is not dry-run, so nothing was applied.")
    return format_report(proposal, rows)


def format_report(proposal: dict, rows: list[dict] | None = None) -> str:
    lines = ["Learning study", proposal.get("why") or "", ""]
    if rows is not None:
        closed = closed_tradable(rows)
        all_stats = summarize(closed)
        lines.append(f"Backtest journal: {len(rows)} setups, {all_stats['n']} closed tradable trades, "
                     f"win rate {_pct(all_stats.get('win_rate'))}, expectancy {_r(all_stats.get('expectancy'))}, "
                     f"max DD {_dd(all_stats.get('max_dd'))}.")
        lines.append("The comparison that matters is the held-out window below, not this full-sample line.")
        lines.append("")
    b = proposal.get("baseline_oos") or {}
    lines.append(f"Held-out baseline ({proposal.get('oos_weeks', 4)} weeks): "
                 f"trades={b.get('n')} win_rate={_pct(b.get('win_rate'))} "
                 f"expectancy={_r(b.get('expectancy'))} max_dd={_dd(b.get('max_dd'))}")
    if proposal.get("adopt_filters"):
        f = proposal.get("filtered_oos") or {}
        lines.append(f"Held-out with filters: trades={f.get('n')} win_rate={_pct(f.get('win_rate'))} "
                     f"expectancy={_r(f.get('expectancy'))} max_dd={_dd(f.get('max_dd'))}")
        for flt in proposal.get("filters") or []:
            lines.append(f"  - {flt.get('why')}")
    else:
        lines.append("No filter passed the held-out check.")
    if proposal.get("adopt_score"):
        m = proposal.get("model_oos") or {}
        lines.append(f"Held-out with score >= {proposal.get('min_score')}: trades={m.get('n')} "
                     f"win_rate={_pct(m.get('win_rate'))} expectancy={_r(m.get('expectancy'))} "
                     f"max_dd={_dd(m.get('max_dd'))}")
    else:
        lines.append("Score gate not adopted (it stays off).")
    lines.append("")
    lines.append(proposal.get("note") or "")
    if proposal.get("status") == "applied":
        lines.append(f"Applied. Previous config.yaml is in {proposal.get('backup')}.")
    elif proposal.get("adoptable"):
        lines.append("Pending in the dashboard. Nothing changes until you press Apply. Live trading will not apply this on its own.")
    return "\n".join(lines).strip()


def _download(cfg: dict, days: int, source: str):
    import market_data
    sym = cfg["symbol"]
    ltf_m, htf_m = int(cfg["ltf_interval"]), int(cfg["htf_interval"])
    end = int(time.time() * 1000)
    start = end - (days + 10) * 24 * 3600 * 1000
    errors = []
    order = [source] + (["okx"] if source != "okx" else [])
    if source != "bybit":
        order.append("bybit") if "bybit" not in order else None
    for src in order:
        try:
            if src == "bybit":
                ltf = market_data.bybit_klines_range(sym, ltf_m, start, end)
                htf = market_data.bybit_klines_range(sym, htf_m, start, end)
            else:
                ltf = market_data.okx_klines_range(sym, ltf_m, start, end)
                htf = market_data.okx_klines_range(sym, htf_m, start, end)
            if len(ltf) > 200 and len(htf) > 50:
                return ltf, htf, src
            errors.append(f"{src}: only {len(ltf)} candles")
        except Exception as exc:
            errors.append(f"{src}: {exc}")
    raise RuntimeError("Could not download candles. " + " | ".join(errors))


def _funding(symbol: str, start_ms: int, end_ms: int):
    try:
        import market_data
        return market_data.bybit_funding_history(symbol, start_ms, end_ms)
    except Exception:
        return []


def dashboard_view(root: Path, cfg: dict) -> dict:
    rows = load_rows(root)
    closed = closed_tradable(rows)
    lc = cfg.get("learning") or {}
    skips = [r for r in rows if r.get("status") == "skipped" or r.get("skip_reason")][-30:]
    scores = []
    model = load_model(root)
    if model:
        for r in closed[-400:]:
            scores.append({"score": score_row(model, r), "win": float(r["r_net"]) > 0, "r": r["r_net"]})
    equity = []
    total = 0.0
    for r in closed:
        total += float(r["r_net"])
        equity.append({"t": r.get("created"), "r": round(total, 4)})
    proposal = load_proposal(root)
    if proposal and proposal.get("model"):
        # The weight vector is large and only needed on disk.
        proposal = {k: v for k, v in proposal.items() if k != "model"}
        proposal["has_model"] = True
    return {
        "summary": summarize(closed),
        "equity": equity[-500:],
        "buckets": _bucket_table(closed),
        "filters": load_filters(root),
        "skips": [{"created": r.get("created"), "side": r.get("side"), "reason": r.get("skip_reason"),
                   "score": r.get("score"), "source": r.get("source")} for r in skips],
        "scores": scores,
        "proposal": proposal,
        "enabled": bool(lc.get("enabled")),
        "min_score": lc.get("min_score") or 0,
        "min_trades": int(lc.get("min_trades", 100)),
        "closed": len(closed),
        "setups": len(rows),
        "due": retrain_due(root),
        "auto_apply_dry_run": bool(lc.get("auto_apply_dry_run")),
        "dry_run": cfg.get("dry_run") is True,
    }


def main():
    import argparse
    import yaml
    ap = argparse.ArgumentParser(description="Build a journal and a walk-forward learning proposal")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--source", choices=["bybit", "okx"], default="bybit")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    print(run_study(ROOT, cfg, days=args.days, source=args.source, on_progress=print))


if __name__ == "__main__":
    main()
