#!/usr/bin/env python3
"""
Backtest the SMC strategy on public SOLUSDT klines (no API keys needed).

  python backtest.py --days 90                 # Bybit public data (default)
  python backtest.py --days 90 --source okx    # OKX SOL-USDT-SWAP data (if Bybit is geo-blocked)
  python backtest.py --days 90 --csv trades.csv

Fills: limit order fills when a later candle trades through the entry price.
Exits: SL/TP on candle highs/lows; if both are hit in one candle, SL is assumed;
on the fill candle only the SL may trigger (conservative). Results in R
(1R = the fixed USDT risk) - gross and net of estimated fees.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

import market_data
import risk
from strategy import run_strategy

ROOT = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--source", choices=["bybit", "okx"], default="bybit")
    ap.add_argument("--csv", default="")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config))
    sym, ltf_m, htf_m = cfg["symbol"], int(cfg["ltf_interval"]), int(cfg["htf_interval"])
    rc = cfg["risk"]

    end = int(time.time() * 1000)
    warm_ms = 10 * 24 * 3600 * 1000            # extra history for HTF structure warm-up
    start = end - args.days * 24 * 3600 * 1000 - warm_ms
    if args.source == "bybit":
        try:
            ltf = market_data.bybit_klines_range(sym, ltf_m, start, end)
            htf = market_data.bybit_klines_range(sym, htf_m, start, end)
        except Exception as e:
            raise SystemExit(f"Could not download Bybit klines ({e}).\nIf Bybit blocks your region/IP, "
                             f"re-run with --source okx (OKX SOL-USDT-SWAP public data).")
    else:
        ltf = market_data.okx_klines_range(sym, ltf_m, start, end)
        htf = market_data.okx_klines_range(sym, htf_m, start, end)
    test_start = end - args.days * 24 * 3600 * 1000
    fmt = lambda ms: pd.to_datetime(ms, unit="ms", utc=True).strftime("%Y-%m-%d %H:%M UTC")
    print(f"Data source: {args.source} | {sym} | LTF {ltf_m}m bars={len(ltf)} | HTF {htf_m}m bars={len(htf)}")
    print(f"Test window: {fmt(test_start)} -> {fmt(int(ltf['ts'].iloc[-1]))}  ({args.days} days, +10d warm-up)")

    rules = risk.InstrumentRules()
    setups = run_strategy(ltf, htf, cfg, rules.tick_size)
    ts = ltf["ts"].to_numpy(np.int64)
    rows = []
    for s in setups:
        if s.status not in ("win", "loss") or ts[s.fill_idx] < test_start:
            continue
        r_gross = (s.exit_price - s.entry) * s.direction / s.risk_per_unit
        sz = risk.position_size(s.entry, s.stop, float(rc["risk_usdt"]), rules, float(rc["leverage"]),
                                bool(rc.get("include_fees_in_risk", True)), float(rc["maker_fee"]), float(rc["taker_fee"]))
        if not sz.ok:
            continue
        # entry = limit (maker); SL and TP attached to the order trigger market exits (taker)
        fees = sz.qty * (s.entry * float(rc["maker_fee"]) + s.exit_price * float(rc["taker_fee"]))
        pnl = sz.qty * (s.exit_price - s.entry) * s.direction - fees
        rows.append(dict(created=fmt(s.created_ts), filled=fmt(int(ts[s.fill_idx])), exit=fmt(int(ts[s.exit_idx])),
                         side=s.side, entry=s.entry, stop=s.stop, tp=s.tp, qty=sz.qty, result=s.status,
                         r_gross=round(r_gross, 3), pnl_usdt=round(pnl, 3),
                         r_net=round(pnl / float(rc["risk_usdt"]), 3), reason=s.reason))
    tdf = pd.DataFrame(rows)
    in_win = [s for s in setups if s.created_ts >= test_start]
    print(f"Setups signalled: {len(in_win)} | filled & closed: {len(tdf)} | "
          f"expired/invalidated (never filled): {sum(1 for s in in_win if s.status in ('expired', 'invalidated'))} | "
          f"still open/pending at end: {sum(1 for s in in_win if s.status in ('pending', 'filled'))}")
    if tdf.empty:
        print("No closed trades in the window.")
        return
    for col, lbl in (("r_gross", "gross (no fees)"), ("r_net", "net of est. fees")):
        r = tdf[col].to_numpy()
        eq = np.cumsum(r)
        dd = float(np.max(np.maximum.accumulate(np.concatenate([[0], eq])) - np.concatenate([[0], eq])))
        print(f"  [{lbl}] trades={len(r)} wins={int((tdf.result == 'win').sum())} "
              f"losses={int((tdf.result == 'loss').sum())} win_rate={100 * (tdf.result == 'win').mean():.1f}% "
              f"total_R={r.sum():+.2f} avg_R={r.mean():+.3f} max_drawdown_R={dd:.2f}")
    print(f"  Net PnL at {rc['risk_usdt']} USDT/trade: {tdf.pnl_usdt.sum():+.2f} USDT | "
          f"longs={int((tdf.side == 'Buy').sum())} shorts={int((tdf.side == 'Sell').sum())}")
    print("\nLast 10 trades:")
    print(tdf[["filled", "side", "entry", "stop", "tp", "qty", "result", "r_net"]].tail(10).to_string(index=False))
    if args.csv:
        tdf.to_csv(args.csv, index=False)
        print(f"\nTrades written to {args.csv}")


if __name__ == "__main__":
    main()
