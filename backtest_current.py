#!/usr/bin/env python3
"""Backtest the strategy shipped on main v1.2.0, using config.yaml as it is.

Does not edit config.yaml. Downloads public SOLUSDT candles (Bybit, then OKX)
and writes reports/current_strategy_backtest.md.

The replay is the bot's default path: 1h bias, 15m sweep plus CHoCH, order-block
limit, 2R target. Breakeven, trail, and partial are off because those defaults
are off. After the replay, the same guards the bot applies are enforced: one
open position, 15 USDT daily loss, 6 fills per UTC day, and a pause for the
rest of the UTC day after 3 consecutive losses. backtest.py itself does not
apply those guards; this script does, because the shipped bot does.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

import learn
import market_data
import risk
from strategy import run_strategy

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "reports" / "current_strategy_backtest.md"
CACHE = ROOT / "state" / "learning" / "cache"
WARMUP_DAYS = 20
WINDOWS = (30, 90, 180, 365)


def _fmt(ms: int) -> str:
    return pd.to_datetime(int(ms), unit="ms", utc=True).strftime("%Y-%m-%d %H:%M UTC")


def _utc_day(ms: int) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d")


def _download(symbol: str, interval: int, start: int, end: int) -> tuple[str, pd.DataFrame]:
    cache = CACHE / f"current_{interval}m.csv"
    source_path = CACHE / f"current_{interval}m.source"
    if cache.exists() and source_path.exists():
        df = pd.read_csv(cache)
        if len(df) and int(df["ts"].iloc[0]) <= start + interval * 60_000 and int(df["ts"].iloc[-1]) >= end - 2 * interval * 60_000:
            return source_path.read_text(encoding="utf-8").strip(), df
    source = "bybit"
    try:
        df = market_data.bybit_klines_range(symbol, interval, start, end)
        if len(df) < 1000:
            raise RuntimeError(f"only {len(df)} Bybit bars")
    except Exception as exc:
        print(f"Bybit {interval}m failed ({exc}). Using OKX.", flush=True)
        source = "okx"
        df = market_data.okx_klines_range(symbol, interval, start, end)
    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache, index=False)
    source_path.write_text(source, encoding="utf-8")
    return source, df


def apply_caps(rows: list[dict], max_daily_r: float, max_trades_per_day: int,
               max_consecutive_losses: int) -> list[dict]:
    """One open position, daily loss, fills per UTC day, and the loss-streak pause."""
    kept = []
    open_until = 0
    day_r: dict[str, float] = {}
    day_n: dict[str, int] = {}
    streak = 0
    streak_day = ""
    blocked_day = None
    ordered = sorted(rows, key=lambda r: (int(r["fill_ts"]), str(r["id"])))
    for row in ordered:
        start = int(row["fill_ts"])
        exit_ts = int(row["exit_ts"])
        if start < open_until:
            continue
        fill_day = _utc_day(start)
        if fill_day != streak_day:
            streak_day = fill_day
            streak = 0
            if blocked_day != fill_day:
                blocked_day = None
        if max_daily_r and day_r.get(fill_day, 0.0) <= -float(max_daily_r):
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


def _stats(rows: list[dict], risk_usdt: float) -> dict:
    if not rows:
        return {"n": 0}
    r = np.array([float(x["r_net"]) for x in rows], float)
    pnl = r * risk_usdt
    wins = r[r > 0]
    losses = r[r <= 0]
    curve = np.concatenate([[0.0], np.cumsum(r)])
    peak = np.maximum.accumulate(curve)
    dd_r = float(np.max(peak - curve))
    streak = longest = 0
    for value in r:
        if value <= 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
    gross_win = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(np.abs(losses.sum())) if len(losses) else 0.0
    longs = [x for x in rows if x["side"] == "Buy"]
    shorts = [x for x in rows if x["side"] == "Sell"]
    return {
        "n": len(rows),
        "wins": int(len(wins)),
        "win_rate": len(wins) / len(rows),
        "avg_r": float(r.mean()),
        "total_r": float(r.sum()),
        "pnl": float(pnl.sum()),
        "dd_r": dd_r,
        "dd_usdt": dd_r * risk_usdt,
        "streak": int(longest),
        "pf": (gross_win / gross_loss) if gross_loss else None,
        "long_n": len(longs),
        "long_r": float(sum(x["r_net"] for x in longs)),
        "short_n": len(shorts),
        "short_r": float(sum(x["r_net"] for x in shorts)),
    }


def _cell(s: dict) -> str:
    if not s or not s.get("n"):
        return "0 | — | — | — | — | — | — | — | —"
    pf = "—" if s["pf"] is None else f"{s['pf']:.2f}"
    sides = f"{s['long_n']} ({s['long_r']:+.2f}R) / {s['short_n']} ({s['short_r']:+.2f}R)"
    return (f"{s['n']} | {100 * s['win_rate']:.1f}% | {s['avg_r']:+.3f} | {s['total_r']:+.2f} | "
            f"{s['pnl']:+.2f} | {s['dd_r']:.2f} / {s['dd_usdt']:.2f} | {s['streak']} | {pf} | {sides}")


def _months(rows: list[dict]) -> str:
    lines = ["| Month | Trades | Win rate | Total R |", "|---|---:|---:|---:|"]
    buckets: dict[str, list] = {}
    for row in rows:
        month = _utc_day(int(row["fill_ts"]))[:7]
        buckets.setdefault(month, []).append(row)
    for month in sorted(buckets):
        group = buckets[month]
        r = [float(x["r_net"]) for x in group]
        wins = sum(1 for x in r if x > 0)
        lines.append(f"| {month} | {len(group)} | {100 * wins / len(group):.1f}% | {sum(r):+.2f} |")
    return "\n".join(lines)


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    rc = cfg["risk"]
    risk_usdt = float(rc["risk_usdt"])
    end = int(time.time() * 1000)
    start = end - (max(WINDOWS) + WARMUP_DAYS) * 24 * 3600 * 1000
    print(f"downloading {cfg['symbol']} 15m and 1h from {_fmt(start)}", flush=True)
    source15, ltf = _download(cfg["symbol"], int(cfg["ltf_interval"]), start, end)
    source60, htf = _download(cfg["symbol"], int(cfg["htf_interval"]), start, end)
    source = source15 if source15 == source60 else f"{source15}/{source60}"
    ltf = ltf.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    htf = htf.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    data_start = int(ltf["ts"].iloc[0])
    data_end = int(ltf["ts"].iloc[-1]) + int(cfg["ltf_interval"]) * 60_000
    span_days = (data_end - data_start) / (24 * 3600 * 1000)
    print(f"source={source} 15m={len(ltf)} 1h={len(htf)} span={span_days:.1f}d {_fmt(data_start)} -> {_fmt(data_end)}", flush=True)

    setups = run_strategy(ltf, htf, cfg, risk.InstrumentRules().tick_size)
    ts = ltf["ts"].to_numpy(np.int64)
    ltf_m = int(cfg["ltf_interval"])
    raw = []
    for setup in setups:
        if setup.status not in ("win", "loss") or setup.fill_idx is None:
            continue
        _gross, net, ok = learn.r_net_of(
            setup, float(rc["maker_fee"]), float(rc["taker_fee"]), risk_usdt,
            leverage=float(rc["leverage"]), include_fees=bool(rc.get("include_fees_in_risk", True)))
        if not ok or net is None:
            continue
        raw.append({
            "id": f"{setup.created_ts}:{setup.side}",
            "fill_ts": int(ts[setup.fill_idx]),
            "exit_ts": int(ts[setup.exit_idx]) + ltf_m * 60_000,
            "r_net": float(net),
            "side": setup.side,
        })
    daily_r = float(rc["max_daily_loss_usdt"]) / risk_usdt
    kept = apply_caps(raw, daily_r, int(rc.get("max_trades_per_day") or 0), int(rc.get("max_consecutive_losses") or 0))
    print(f"closed {len(raw)} kept {len(kept)}", flush=True)

    available = []
    stats = {}
    for days in WINDOWS:
        window_start = data_end - days * 24 * 3600 * 1000
        if window_start < data_start - 24 * 3600 * 1000:
            available.append((days, None, window_start))
            continue
        rows = [r for r in kept if int(r["fill_ts"]) >= window_start]
        stats[days] = _stats(rows, risk_usdt)
        available.append((days, stats[days], window_start))
        print(f"{days}d {_cell(stats[days])}", flush=True)

    longest = max(days for days, s, _ in available if s)
    long_rows = [r for r in kept if int(r["fill_ts"]) >= data_end - longest * 24 * 3600 * 1000]

    header = ("| Window | Trades | Win rate | Avg R | Total R | P&L USDT | Max DD R / USDT | "
              "Losing streak | Profit factor | Long / short |")
    rule = "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|"
    lines = [header, rule]
    limited = []
    for days, s, window_start in available:
        if s is None:
            lines.append(f"| {days}d | — | — | — | — | — | — | — | — | data starts {_fmt(data_start)} |")
            limited.append(f"{days}d")
        else:
            lines.append(f"| last {days}d ({_fmt(window_start)} → {_fmt(data_end)}) | {_cell(s)} |")

    text = f"""# Current strategy backtest

Shipped defaults from `config.yaml` on main v1.2.0. This run does not change that file.

- Symbol: **{cfg['symbol']}** only. Higher timeframe {cfg['htf_interval']}m bias, entry timeframe {cfg['ltf_interval']}m.
- Rules: higher-timeframe structure bias, 15m liquidity sweep, CHoCH required, order-block limit, target at **{cfg['strategy']['rr']:g}R**. Breakeven, trail, and partial take-profit are off. News filter is off.
- Risk: **{risk_usdt:g} USDT** per trade, fees inside that risk. Daily loss cap **{rc['max_daily_loss_usdt']:g} USDT**. At most **{rc['max_trades_per_day']}** fills per UTC day. After **{rc['max_consecutive_losses']}** consecutive losses, no new entry for the rest of that UTC day. One position at a time.
- Data: **{source}** public {cfg['symbol']} candles, {_fmt(data_start)} → {_fmt(data_end)} ({span_days:.0f} days including {WARMUP_DAYS} days of warm-up that cannot count as a reported fill). {len(ltf)} entry bars, {len(htf)} bias bars.
- Fees: maker {rc['maker_fee']} on the limit entry, taker {rc['taker_fee']} on the stop or target, same sizing as `backtest.py`. A win is a trade with positive R after those fees. **Slippage is not modelled. Funding is not modelled.**
- USDT P&L is net R times {risk_usdt:g}. Max drawdown in USDT is the R drawdown times {risk_usdt:g}. Profit factor is the sum of winning R divided by the absolute sum of losing R.
- The plain `backtest.py` replay does not apply the daily cap, the trade-count cap, or the loss-streak pause. This report does, because the bot shipped in v1.2.0 does. Closed signals before those guards: {len(raw)}. After the guards: {len(kept)}. On this sample the extra guards removed {len(raw) - len(kept)} trade(s); the strategy replay already allows only one position.

## Windows

{chr(10).join(lines)}

## Month by month (last {longest} days)

Months are the UTC month of the fill. The first month starts on the first reported fill's window boundary, and the last month ends at the last candle, so those two months are partial.

{_months(long_rows)}

Past results are one path of SOLUSDT. They are not a forecast.
"""
    if limited:
        text += f"\nWindows not fully covered because the download starts {_fmt(data_start)}: {', '.join(limited)}.\n"
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {REPORT}", flush=True)


if __name__ == "__main__":
    main()
