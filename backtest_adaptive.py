#!/usr/bin/env python3
"""Compare fixed timeframe pairs with the opt-in adaptive regime rule.

  python backtest_adaptive.py --days 90 --source okx

Writes reports/adaptive_backtest.md. Does not change config.yaml.
Fees match backtest.py (maker 0.02%, taker 0.055%). Slippage is not modelled.
Every variant is passed through the same caps: 5 USDT risk, one open position,
15 USDT daily loss, 6 fills per UTC day, and a pause after 3 losses.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd
import yaml

import adaptive
import market_data

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "reports" / "adaptive_backtest.md"


def _fmt_ts(ms: int) -> str:
    return pd.to_datetime(int(ms), unit="ms", utc=True).strftime("%Y-%m-%d %H:%M UTC")


def _load(source: str, symbol: str, start: int, end: int) -> tuple[str, pd.DataFrame]:
    if source == "bybit":
        try:
            df = market_data.bybit_klines_range(symbol, 5, start, end)
            if len(df) < 1000:
                raise RuntimeError(f"only {len(df)} Bybit 5m bars")
            return "bybit", df
        except Exception as exc:
            print(f"Bybit 5m download failed ({exc}). Falling back to OKX.")
    df = market_data.okx_klines_range(symbol, 5, start, end)
    return "okx", df


def _cell(stats: dict) -> str:
    if not stats or not stats.get("n"):
        return "0 | — | — | — | —"
    wr = 100.0 * stats["win_rate"]
    return (f"{stats['n']} | {wr:.1f}% | {stats['expectancy']:+.3f} | "
            f"{stats['total_r']:+.2f} | {stats['max_dd']:.2f}")


def _beats(challenger: dict, baseline: dict) -> str:
    if challenger.get("n", 0) < 8 or baseline.get("n", 0) < 8:
        return ("too few out-of-sample trades to claim an edge "
                f"(adaptive {challenger.get('n', 0)}, baseline {baseline.get('n', 0)}; floor 8)")
    better_exp = challenger["expectancy"] > baseline["expectancy"]
    better_total = challenger["total_r"] > baseline["total_r"]
    dd_ok = challenger["max_dd"] <= baseline["max_dd"] * 1.2 + 1e-9
    if better_exp and better_total and dd_ok:
        return "adaptive beat the 1h/15m baseline out of sample on expectancy and total R, without a drawdown more than 20% worse"
    parts = []
    if not better_exp:
        parts.append("expectancy was not higher")
    if not better_total:
        parts.append("total R was not higher")
    if not dd_ok:
        parts.append("drawdown was more than 20% worse")
    return "adaptive did not beat the 1h/15m baseline out of sample: " + "; ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--warmup-days", type=int, default=20)
    ap.add_argument("--source", choices=["bybit", "okx"], default="bybit")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config, encoding="utf-8"))
    symbol = cfg["symbol"]
    end = int(time.time() * 1000)
    test_start = end - args.days * 24 * 3600 * 1000
    split_ms = test_start + 60 * 24 * 3600 * 1000
    start = test_start - args.warmup_days * 24 * 3600 * 1000

    cache = ROOT / "state" / "learning" / "cache" / f"adaptive_5m_{args.days}d.csv"
    if cache.exists():
        raw = pd.read_csv(cache)
        side = cache.with_suffix(".source")
        source = side.read_text(encoding="utf-8").strip() if side.exists() else "cache"
        last = int(raw["ts"].iloc[-1]) if len(raw) else 0
        if last < end - 2 * 24 * 3600 * 1000:
            print(f"cache ends {_fmt_ts(last)}, redownloading", flush=True)
            source, raw = _load(args.source, symbol, start, end)
            cache.parent.mkdir(parents=True, exist_ok=True)
            raw.to_csv(cache, index=False)
            side.write_text(source, encoding="utf-8")
        else:
            print(f"loaded cache {cache} ({len(raw)} bars, {source})", flush=True)
    else:
        source, raw = _load(args.source, symbol, start, end)
        cache.parent.mkdir(parents=True, exist_ok=True)
        raw.to_csv(cache, index=False)
        cache.with_suffix(".source").write_text(source, encoding="utf-8")
        print(f"cached {len(raw)} bars to {cache}", flush=True)
    frames = {
        5: raw,
        15: adaptive.resample(raw, 15),
        60: adaptive.resample(raw, 60),
        240: adaptive.resample(raw, 240),
    }
    print(f"source={source} 5m={len(frames[5])} 15m={len(frames[15])} 1h={len(frames[60])} 4h={len(frames[240])}")
    print(f"window {_fmt_ts(test_start)} -> {_fmt_ts(int(frames[5]['ts'].iloc[-1]))}")

    specs = (
        ("1h/15m baseline", 60, 15, 2.0),
        ("1h/5m", 60, 5, 1.5),
        ("4h/1h", 240, 60, 2.5),
    )
    raw = {}
    for name, htf_m, ltf_m, rr in specs:
        print(f"replaying {name} ...", flush=True)
        raw[name] = adaptive.closed_trades(
            frames[ltf_m], frames[htf_m], adaptive._with_pair(cfg, htf_m, ltf_m, rr), test_start)
        print(f"  closed before caps: {len(raw[name])}", flush=True)
    variants = {name: adaptive.apply_caps(rows) for name, rows in raw.items()}
    pooled = [dict(row) for rows in raw.values() for row in rows]
    adaptive_rows = adaptive.adaptive_trades(frames, cfg, test_start, pooled=pooled)
    fixed_only = {name: rows for name, rows in variants.items()}
    picked = adaptive.select_pair_on_fit(fixed_only, test_start, split_ms)

    order = ["1h/15m baseline", "1h/5m", "4h/1h", "adaptive (pre-registered)"]
    all_rows = {**variants, "adaptive (pre-registered)": adaptive_rows}
    splits = {name: adaptive.summarize_split(rows, test_start, split_ms) for name, rows in all_rows.items()}
    if picked:
        order.append(f"fitted fixed pair: {picked}")
        splits[order[-1]] = splits[picked]

    table = adaptive.regime_table(frames[60], frames[240])
    in_window = table[(table["close_ts"] >= test_start) & (table["close_ts"] <= int(frames[5]["ts"].iloc[-1]) + 5 * 60_000)]
    counts = in_window["name"].value_counts().to_dict()
    oos_verdict = _beats(splits["adaptive (pre-registered)"]["oos"], splits["1h/15m baseline"]["oos"])
    if oos_verdict.startswith("adaptive beat"):
        recommendation = "Optional dry-run experiment only. Do not enable it on a live account from this one holdout."
    else:
        recommendation = "Do not install, including in dry run. Leave `adaptive_mode` false."

    def table_for(part: str) -> str:
        lines = ["| Variant | Trades | Win rate | Avg R | Total R | Max DD (R) |",
                 "|---|---:|---:|---:|---:|---:|"]
        for name in order:
            lines.append(f"| {name} | {_cell(splits[name][part])} |")
        return "\n".join(lines)

    taken = {}
    for row in adaptive_rows:
        taken[row.get("regime") or "?"] = taken.get(row.get("regime") or "?", 0) + 1
    regime_lines = ", ".join(f"{k} {counts.get(k, 0)} hours" for k in
                             ("strong_trend", "trend", "mixed", "range", "high_vol", "unknown"))
    taken_lines = ", ".join(f"{k} {v}" for k, v in sorted(taken.items())) or "none"
    cap_bits = []
    for name, _htf, _ltf, _rr in specs:
        cap_bits.append(f"{name} {len(raw[name])}→{len(variants[name])}")
    cap_line = ", ".join(cap_bits)
    if picked:
        is_s = splits[picked]["is"]
        oos_s = splits[picked]["oos"]
        base_oos = splits["1h/15m baseline"]["oos"]
        fit_note = (
            f"The in-sample picker chose **{picked}** ({is_s['n']} trades, expectancy {is_s['expectancy']:+.3f}R). "
            f"Out of sample that frozen pair was expectancy {oos_s['expectancy']:+.3f}R over {oos_s['n']} trades "
            f"(total {oos_s['total_r']:+.2f}R, drawdown {oos_s['max_dd']:.2f}R), against the baseline's "
            f"{base_oos['expectancy']:+.3f}R over {base_oos['n']} trades. The pair that looked best on the fit window "
            f"was not the best out-of-sample expectancy."
        )
    else:
        fit_note = "No fixed pair had 8 in-sample trades, so nothing was frozen."

    text = f"""# Adaptive timeframe backtest

Generated by `python backtest_adaptive.py`. This file is a research result. It does not turn the mode on.

- Source: **{source}** public {symbol} 5-minute candles, resampled to 15m, 1h and 4h on clock buckets. Incomplete buckets are dropped.
- Test window: {_fmt_ts(test_start)} → {_fmt_ts(int(frames[5]['ts'].iloc[-1]))} ({args.days} days), plus {args.warmup_days} days of warm-up that cannot fill a trade.
- Walk-forward split: first 60 days are in-sample ({_fmt_ts(test_start)} → {_fmt_ts(split_ms)}), last 30 days are out of sample ({_fmt_ts(split_ms)} onward). Fills are counted by fill time.
- Fees: maker 0.0002 on the limit entry, taker 0.00055 on the stop or target, included in the 5 USDT risk the same way as `backtest.py`. **Slippage is not modelled.** Funding is not modelled.
- Caps on every variant, including the baseline: exactly 5 USDT risk, one open position, stop new entries after 3R (15 USDT) realized loss on that UTC day, at most 6 fills per UTC day, and a pause for the rest of the UTC day after 3 consecutive losses. Breakeven, trail, and partial take-profit are off so the timeframe choice is the only difference.
- The adaptive thresholds (ADX 18 / 22 / 30, ATR percentile 25 / 75 / 80 / 85, and the three pairs with R targets 2.0 / 1.5 / 2.5) were written down before this run. They were **not** searched on these 90 days. The row "fitted fixed pair" is the opposite: it is the fixed pair with the best in-sample expectancy and at least 8 in-sample trades ({picked or "none qualified"}), then frozen. That row exists to show selection bias. It is not the adaptive rule.
- Trades removed by the shared caps (closed signals → kept): {cap_line}. A baseline that barely changes means this table is comparable to the earlier uncapped 1h/15m study.

Regime hours inside the test window (one label per closed 1h bar): {regime_lines}.

Adaptive trades by the regime that allowed them: {taken_lines}.

## Full {args.days} days

{table_for("full")}

## In sample (first 60 days)

{table_for("is")}

## Out of sample (last 30 days)

{table_for("oos")}

## Out-of-sample verdict

{oos_verdict}.

{fit_note}

The full-window adaptive result is not the decision. The rule can still be overfit: three pairs, three R targets, and the skip thresholds were chosen by hand from how this strategy already behaves, on the same SOL market the earlier 90-day studies used. A single 30-day holdout is one path, not a distribution. Slice drawdowns are measured inside that slice only. Out-of-sample max drawdown is not the drawdown from the in-sample equity peak.

**Recommendation: {recommendation}**

`adaptive_mode` stays **false** in `config.yaml`. Default entries stay 1h bias and 15m. Dry run stays on. Live learning stays proposal-only. Nothing in this report places an order.
"""
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {REPORT}")


if __name__ == "__main__":
    main()
