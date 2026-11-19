#!/usr/bin/env python3
"""Backtest the opt-in 1h/4h fair-value-gap model. Does not change config.yaml.

  python backtest_fvg_mtf.py

Uses the OKX 5m cache from the adaptive study when it is present, otherwise
downloads OKX (Bybit first, which this host has been blocking). Fees, the 5 USDT
risk, the 15 USDT daily loss, one position, 6 fills a day, and the 3-loss pause
match that study. Slippage is not modelled.
"""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

import fvg_mtf
import learn
import market_data
from strategy import run_strategy

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "reports" / "fvg_mtf_backtest.md"
CACHE = ROOT / "state" / "learning" / "cache" / "adaptive_5m_90d.csv"

# Published on the adaptive-study branch from this same cache family.
# Baseline there was recomputed; these two rows are cited, not refit.
ADAPTIVE_REF = {
    "full": {"n": 34, "win_rate": 0.471, "expectancy": 0.108, "total_r": 3.69, "max_dd": 5.45},
    "is": {"n": 20, "win_rate": 0.450, "expectancy": 0.064, "total_r": 1.27, "max_dd": 5.45},
    "oos": {"n": 14, "win_rate": 0.500, "expectancy": 0.173, "total_r": 2.42, "max_dd": 2.34},
}


def _fmt(ms: int) -> str:
    return pd.to_datetime(int(ms), unit="ms", utc=True).strftime("%Y-%m-%d %H:%M UTC")


def _load() -> tuple[str, pd.DataFrame]:
    if CACHE.exists():
        df = pd.read_csv(CACHE)
        side = CACHE.with_suffix(".source")
        source = side.read_text(encoding="utf-8").strip() if side.exists() else "cache"
        return source, df
    end = int(time.time() * 1000)
    start = end - 110 * 24 * 3600 * 1000
    try:
        df = market_data.bybit_klines_range("SOLUSDT", 5, start, end)
        if len(df) < 1000:
            raise RuntimeError(f"only {len(df)} Bybit bars")
        return "bybit", df
    except Exception as exc:
        print(f"Bybit failed ({exc}). Using OKX.", flush=True)
        return "okx", market_data.okx_klines_range("SOLUSDT", 5, start, end)


def _pack(rows: list[dict]) -> dict:
    stats = learn.summarize(rows)
    rrs = [float(r["planned_rr"]) for r in rows if r.get("planned_rr") is not None]
    stats["avg_planned_rr"] = None if not rrs else round(sum(rrs) / len(rrs), 3)
    return stats


def _splits(rows: list[dict], test_start: int, split_ms: int) -> dict:
    def sub(pred):
        return _pack([r for r in rows if pred(int(r["fill_ts"]))])
    return {
        "full": sub(lambda t: t >= test_start),
        "is": sub(lambda t: test_start <= t < split_ms),
        "oos": sub(lambda t: t >= split_ms),
    }


def _cell(stats: dict) -> str:
    if not stats or not stats.get("n"):
        return "0 | — | — | — | — | —"
    rr = stats.get("avg_planned_rr")
    rr_s = "—" if rr is None else f"{rr:.2f}"
    return (f"{stats['n']} | {100 * stats['win_rate']:.1f}% | {stats['expectancy']:+.3f} | "
            f"{stats['total_r']:+.2f} | {stats['max_dd']:.2f} | {rr_s}")


def _baseline(m15, h1, cfg, maker, taker) -> list[dict]:
    c = yaml.safe_load(yaml.safe_dump(cfg))
    c["ltf_interval"] = 15
    c["htf_interval"] = 60
    c["strategy"]["rr"] = 2.0
    c["strategy"]["breakeven_at_r"] = 0
    c["strategy"]["trail_mode"] = "off"
    c["strategy"]["partial_tp_pct"] = 0
    c["risk"]["risk_usdt"] = 5.0
    setups = run_strategy(m15, h1, c, 0.01)
    ts = m15["ts"].to_numpy(np.int64)
    by = {}
    for setup in setups:
        by.setdefault(int(setup.created_ts), []).append(setup)
    rows = []
    for row in learn.rows_from_setups(setups, c, source="backtest"):
        if row.get("status") not in ("win", "loss") or row.get("r_net") is None or not row.get("tradable"):
            continue
        setup = next((s for s in by.get(int(row["created_ts"]), [])
                      if s.status in ("win", "loss") and s.fill_idx is not None), None)
        if setup is None:
            continue
        dist = abs(setup.entry - (setup.initial_stop or setup.stop)) or setup.risk_per_unit
        row = dict(row)
        row["fill_ts"] = int(ts[setup.fill_idx])
        row["exit_ts"] = int(ts[setup.exit_idx]) + 15 * 60_000
        row["planned_rr"] = abs(setup.tp - setup.entry) / dist
        row["id"] = f"base:{setup.created_ts}"
        rows.append(row)
    return rows


def _beats(challenger: dict, baseline: dict) -> str:
    if challenger.get("n", 0) < 8 or baseline.get("n", 0) < 8:
        return (f"too few out-of-sample trades to claim an edge "
                f"(variant {challenger.get('n', 0)}, baseline {baseline.get('n', 0)}; floor 8)")
    better_exp = challenger["expectancy"] > baseline["expectancy"]
    better_total = challenger["total_r"] > baseline["total_r"]
    dd_ok = challenger["max_dd"] <= baseline["max_dd"] * 1.2 + 1e-9
    if better_exp and better_total and dd_ok:
        return "the pre-registered variant beat the 1h/15m baseline out of sample on expectancy and total R, without a drawdown more than 20% worse"
    missing = []
    if not better_exp:
        missing.append("expectancy was not higher")
    if not better_total:
        missing.append("total R was not higher")
    if not dd_ok:
        missing.append("drawdown was more than 20% worse")
    return "the pre-registered variant did not beat the 1h/15m baseline out of sample: " + "; ".join(missing)


def _svg(m5: pd.DataFrame, row: dict, path: Path, title: str) -> None:
    a = max(0, int(row["ob_idx"]) - 18)
    b = min(len(m5) - 1, int(row["exit_idx"]) + 6)
    sl = m5.iloc[a:b + 1].reset_index(drop=True)
    highs = sl["high"].to_numpy(float)
    lows = sl["low"].to_numpy(float)
    pad = max(0.15, (float(np.max(highs)) - float(np.min(lows))) * 0.08)
    lo = min(float(np.min(lows)), row["stop"], row["tp"], row["entry"]) - pad
    hi = max(float(np.max(highs)), row["stop"], row["tp"], row["entry"]) + pad
    if hi <= lo:
        hi = lo + 1
    width, height, left, top, plot_w, plot_h = 920, 420, 56, 36, 840, 340
    n = len(sl)

    def x(i):
        return left + (i + 0.5) * plot_w / max(n, 1)

    def y(price):
        return top + (hi - price) / (hi - lo) * plot_h

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#0e1116"/>',
        f'<text x="56" y="22" fill="#e8eaed" font-family="sans-serif" font-size="14">{title}</text>',
    ]
    y1, y2 = y(row["fvg_top"]), y(row["fvg_bottom"])
    parts.append(f'<rect x="{left}" y="{min(y1, y2):.1f}" width="{plot_w}" height="{abs(y2 - y1):.1f}" fill="#3d7ea6" opacity="0.25"/>')
    for i, bar in sl.iterrows():
        color = "#3dd68c" if bar["close"] >= bar["open"] else "#f07178"
        x0 = x(i)
        parts.append(f'<line x1="{x0:.1f}" y1="{y(bar["high"]):.1f}" x2="{x0:.1f}" y2="{y(bar["low"]):.1f}" stroke="{color}" stroke-width="1"/>')
        body_top = y(max(bar["open"], bar["close"]))
        body_h = max(1.0, abs(y(bar["open"]) - y(bar["close"])))
        parts.append(f'<rect x="{x0 - 3:.1f}" y="{body_top:.1f}" width="6" height="{body_h:.1f}" fill="{color}"/>')
    for price, color, name in ((row["entry"], "#e8eaed", "entry"), (row["stop"], "#f07178", "stop"), (row["tp"], "#3dd68c", "target")):
        yy = y(price)
        parts.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{left + plot_w}" y2="{yy:.1f}" stroke="{color}" stroke-dasharray="4 3"/>')
        parts.append(f'<text x="{left + plot_w - 120}" y="{yy - 4:.1f}" fill="{color}" font-family="sans-serif" font-size="11">{name} {price:.2f}</text>')
    parts.append("</svg>")
    path.write_text("".join(parts), encoding="utf-8")


def _png_from_svg_data(m5, row, path: Path, title: str) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    a = max(0, int(row["ob_idx"]) - 18)
    b = min(len(m5) - 1, int(row["exit_idx"]) + 6)
    sl = m5.iloc[a:b + 1].reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(10.5, 4.6), dpi=120)
    fig.patch.set_facecolor("#0e1116")
    ax.set_facecolor("#0e1116")
    for i, bar in sl.iterrows():
        color = "#3dd68c" if bar["close"] >= bar["open"] else "#f07178"
        ax.vlines(i, bar["low"], bar["high"], color=color, linewidth=1)
        ax.bar(i, abs(bar["close"] - bar["open"]), bottom=min(bar["open"], bar["close"]), width=0.6, color=color)
    ax.axhspan(row["fvg_bottom"], row["fvg_top"], color="#3d7ea6", alpha=0.25, zorder=0)
    pad = max(0.15, (float(sl["high"].max()) - float(sl["low"].min())) * 0.15)
    lo = min(float(sl["low"].min()), row["stop"], row["tp"], row["entry"]) - pad
    hi = max(float(sl["high"].max()), row["stop"], row["tp"], row["entry"]) + pad
    ax.set_ylim(lo, hi)
    ax.axhline(row["entry"], color="#e8eaed", ls="--", lw=1, label=f"entry {row['entry']:.2f}")
    ax.axhline(row["stop"], color="#f07178", ls="--", lw=1, label=f"stop {row['stop']:.2f}")
    ax.axhline(row["tp"], color="#3dd68c", ls="--", lw=1, label=f"target {row['tp']:.2f}")
    ax.set_title(title, color="#e8eaed")
    ax.tick_params(colors="#e8eaed")
    ax.legend(facecolor="#0e1116", edgecolor="#444", labelcolor="#e8eaed", fontsize=8)
    for spine in ax.spines.values():
        spine.set_color("#444")
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return True


def main():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    source, raw = _load()
    raw = raw.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    frames = {5: raw, 15: fvg_mtf.resample(raw, 15), 60: fvg_mtf.resample(raw, 60), 240: fvg_mtf.resample(raw, 240)}
    end_close = int(frames[5]["ts"].iloc[-1]) + 5 * 60_000
    test_start = end_close - 90 * 24 * 3600 * 1000
    split_ms = test_start + 60 * 24 * 3600 * 1000
    maker = float(cfg["risk"]["maker_fee"])
    taker = float(cfg["risk"]["taker_fee"])
    print(f"source={source} 5m={len(frames[5])} window {_fmt(test_start)} -> {_fmt(end_close)}", flush=True)

    specs = []
    for sources, source_name in (("1h", "1h gaps"), ("4h", "4h gaps"), ("both", "1h+4h gaps")):
        for buffer, buffer_name in (("fixed", "$0.05 buffer"), ("pct", "0.05% buffer")):
            for min_rr, filt_name in ((1.0, "min 1R"), (0.0, "no R filter")):
                specs.append((f"{source_name}, {buffer_name}, {filt_name}", sources, buffer, min_rr))

    results = {}
    raw_counts = {}
    for label, sources, buffer, min_rr in specs:
        print(f"replaying {label} ...", flush=True)
        played = fvg_mtf.replay(frames[5], frames[60], frames[240], cfg, sources=sources, buffer=buffer, min_rr=min_rr)
        rows = fvg_mtf.closed_rows(frames[5], played.setups, maker, taker)
        raw_counts[label] = (len(rows), played.skipped_rr, played.skipped_no_target, played.skipped_no_ob, played.skipped_entry, played.skipped_size)
        results[label] = fvg_mtf.apply_caps(rows)
        print(f"  closed {len(rows)} kept {len(results[label])} skipped_rr {played.skipped_rr}", flush=True)

    print("replaying 1h/15m baseline ...", flush=True)
    base_raw = _baseline(frames[15], frames[60], cfg, maker, taker)
    results["1h/15m baseline"] = fvg_mtf.apply_caps(base_raw)
    print(f"  closed {len(base_raw)} kept {len(results['1h/15m baseline'])}", flush=True)

    preset = "1h+4h gaps, 0.05% buffer, min 1R"
    order = [label for label, _, _, _ in specs] + ["1h/15m baseline"]
    splits = {name: _splits(rows, test_start, split_ms) for name, rows in results.items()}

    def table(part: str) -> str:
        lines = ["| Variant | Trades | Win rate | Avg R | Total R | Max DD (R) | Avg planned R |",
                 "|---|---:|---:|---:|---:|---:|---:|"]
        for name in order:
            lines.append(f"| {name} | {_cell(splits[name][part])} |")
        ref = dict(ADAPTIVE_REF[part])
        ref["avg_planned_rr"] = None
        lines.append(f"| adaptive study (cited) | {_cell(ref)} |")
        return "\n".join(lines)

    verdict = _beats(splits[preset]["oos"], splits["1h/15m baseline"]["oos"])
    if verdict.startswith("the pre-registered variant beat"):
        recommendation = "Optional dry-run experiment only. Do not turn it on for a live account from this one holdout."
    else:
        recommendation = "Do not install, including in dry run. Leave `strategy_variant: default`."

    # Charts: first out-of-sample win and loss of the pre-registered rules, else the nearest closed trade.
    preset_rows = [r for r in results[preset] if int(r["fill_ts"]) >= split_ms]
    if not preset_rows:
        preset_rows = [r for r in results[preset] if int(r["fill_ts"]) >= test_start]
    win = next((r for r in preset_rows if r["r_net"] > 0), None)
    loss = next((r for r in preset_rows if r["r_net"] <= 0), None)
    chart_notes = []
    reports = ROOT / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    for kind, row in (("win", win), ("loss", loss)):
        if row is None:
            chart_notes.append(f"No {kind} was available to chart for the pre-registered rules.")
            continue
        when = "out of sample" if int(row["fill_ts"]) >= split_ms else "in the 90-day window"
        title = (f"{kind} {when}: {row['source_tf']} {'long' if row['direction'] > 0 else 'short'} "
                 f"planned {row['planned_rr']:.2f}R, result {row['r_net']:+.2f}R")
        svg = reports / f"fvg_mtf_example_{kind}.svg"
        png = reports / f"fvg_mtf_example_{kind}.png"
        _svg(frames[5], row, svg, title)
        drew = _png_from_svg_data(frames[5], row, png, title)
        chart_notes.append(f"{title}. Chart: `{png.name}`." if drew else f"{title}. Chart: `{svg.name}`.")

    oos_ranked = sorted(
        ((name, splits[name]["oos"]) for name in order if name != "1h/15m baseline"),
        key=lambda item: (-1e9 if item[1]["expectancy"] is None else item[1]["expectancy"]),
        reverse=True,
    )
    best_name, best_oos = oos_ranked[0]
    skip_line = ", ".join(
        f"{label}: closed {raw_counts[label][0]}, kept {len(results[label])}, "
        f"rejected by min R {raw_counts[label][1]}, no target {raw_counts[label][2]}"
        for label, _, _, _ in specs if "min 1R" in label)

    text = f"""# 1h/4h fair-value-gap backtest

Generated by `python backtest_fvg_mtf.py`. This does not turn the variant on.

- Source: **{source}** SOLUSDT 5-minute candles, resampled to 15m, 1h and 4h. Incomplete buckets are dropped.
- Window: {_fmt(test_start)} → {_fmt(int(frames[5]['ts'].iloc[-1]))}. Fills before {_fmt(test_start)} are warm-up. The split is the first 60 days in sample and the last 30 out of sample, by fill time.
- Fees: maker 0.0002, taker 0.00055, inside a fixed **5 USDT** risk, same sizing as `backtest.py`. **Slippage and funding are not modelled.**
- Caps on every row, including the baseline: one open position, stop new entries after 3R (15 USDT) realized on that UTC day, at most 6 fills a day, pause after 3 consecutive losses.
- Pre-registered rules, chosen before this run and used if `strategy_variant` is ever set to `fvg_mtf`: **1h and 4h gaps together, 0.05% stop buffer, skip planned reward below 1R.** The other rows are a sensitivity check. Picking the best of them after seeing the holdout is overfitting.
- Entry is the proximal extreme of the last opposite-color 5m candle before the CHoCH (its high for a long, its low for a short). The stop is the other extreme of that same candle, plus either $0.05 or 0.05% of that extreme. A 1h tap has 24 five-minute bars (2 hours) to print the CHoCH; a 4h tap has 48 bars (4 hours). Trading through the far side of the gap cancels the idea. The limit expires after 12 bars.
- The target is the nearer of an unmitigated 1h/4h swing and the near edge of an opposing unmitigated gap, using only levels that exist on the signal close. Average planned R is that distance divided by the stop distance, before fees. Win rate and avg R are after fees.

Planned reward averaged about 6R to 14R, and the win rate stayed under 17% on the full window, so those far targets were not hit often enough to pay for the stops.

The rejection counts below include warm-up fills. The tables count only fills inside the 90-day window. Rejections before the caps, for the min-1R rows: {skip_line}.

## Full 90 days

{table("full")}

## In sample (first 60 days)

{table("is")}

## Out of sample (last 30 days)

{table("oos")}

## Out-of-sample verdict

{verdict}.

The pre-registered row is `{preset}`. The highest out-of-sample expectancy among the twelve sensitivity rows was **{best_name}** ({_cell(best_oos)}). That ranking uses the holdout, so it is not a setting to ship. The adaptive row is cited from the earlier study on this same OKX cache (34 trades, +0.108R full window, +0.173R on the last 30 days). It was not refit here. A full-window profit that disappears, or shrinks, on the last 30 days is the overfitting warning. Twelve rule combinations on one 30-day path can crown a winner by luck.

## Example trades

The charts are the first out-of-sample win and the first out-of-sample loss under the pre-registered rules, in fill order. They are illustrations, not the best trades.

{chr(10).join('- ' + note for note in chart_notes)}

![Example win](fvg_mtf_example_win.png)

![Example loss](fvg_mtf_example_loss.png)

**Recommendation: {recommendation}**

`strategy_variant` stays **default**. Entries stay 1h bias and 15m. Dry run stays on. Risk stays 5 USDT. Nothing in this report places an order.
"""
    REPORT.write_text(text, encoding="utf-8")
    print(text)
    print(f"wrote {REPORT}", flush=True)


def _cell_r(stats: dict) -> str:
    if not stats or not stats.get("n"):
        return "0 | — | — | — | —"
    return (f"{stats['n']} | {100 * stats['win_rate']:.1f}% | {stats['expectancy']:+.3f} | "
            f"{stats['total_r']:+.2f} | {stats['max_dd']:.2f}")


def run_rr_cap():
    """Append the fixed-R cap study. Entry, stop, data, fees, and caps are unchanged.

    Pre-registered before the run: 1h+4h, 0.05% buffer, 2R cap, minimum 1R.
    """
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    source, raw = _load()
    raw = raw.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    frames = {5: raw, 15: fvg_mtf.resample(raw, 15), 60: fvg_mtf.resample(raw, 60), 240: fvg_mtf.resample(raw, 240)}
    end_close = int(frames[5]["ts"].iloc[-1]) + 5 * 60_000
    test_start = end_close - 90 * 24 * 3600 * 1000
    split_ms = test_start + 60 * 24 * 3600 * 1000
    maker = float(cfg["risk"]["maker_fee"])
    taker = float(cfg["risk"]["taker_fee"])
    preset = "1h+4h, 0.05%, 2R cap, min 1R"
    print(f"rr-cap source={source} window {_fmt(test_start)} -> {_fmt(end_close)}", flush=True)

    specs = []
    for sources, source_name in (("1h", "1h"), ("4h", "4h"), ("both", "1h+4h")):
        for rr in (1.5, 2.0, 3.0):
            for buffer, buffer_name in (("fixed", "$0.05"), ("pct", "0.05%")):
                for min_rr, filt_name in ((1.0, "min 1R"), (0.0, "no R filter")):
                    label = f"{source_name}, {buffer_name}, {rr:g}R cap, {filt_name}"
                    specs.append((label, sources, buffer, rr, min_rr))

    results = {}
    for label, sources, buffer, rr, min_rr in specs:
        print(f"replaying {label} ...", flush=True)
        played = fvg_mtf.replay(frames[5], frames[60], frames[240], cfg, sources=sources,
                                buffer=buffer, min_rr=min_rr, rr_cap=rr)
        rows = fvg_mtf.apply_caps(fvg_mtf.closed_rows(frames[5], played.setups, maker, taker))
        results[label] = rows
        print(f"  kept {len(rows)}", flush=True)

    print("replaying 1h/15m baseline ...", flush=True)
    results["1h/15m baseline"] = fvg_mtf.apply_caps(_baseline(frames[15], frames[60], cfg, maker, taker))
    splits = {name: _splits(rows, test_start, split_ms) for name, rows in results.items()}

    def _sig(rows):
        return [(int(r["fill_ts"]), round(float(r["r_net"]), 3)) for r in rows]

    identical = True
    for sources, source_name in (("1h", "1h"), ("4h", "4h"), ("both", "1h+4h")):
        for rr in (1.5, 2.0, 3.0):
            for min_rr, filt_name in ((1.0, "min 1R"), (0.0, "no R filter")):
                a = _sig(results[f"{source_name}, $0.05, {rr:g}R cap, {filt_name}"])
                b = _sig(results[f"{source_name}, 0.05%, {rr:g}R cap, {filt_name}"])
                if a != b:
                    identical = False
    shown = [label for label, _, buffer, _, _ in specs if buffer == "pct" or not identical]
    shown.append("1h/15m baseline")

    def table(part: str) -> str:
        lines = ["| Variant | Trades | Win rate | Avg R | Total R | Max DD (R) |",
                 "|---|---:|---:|---:|---:|---:|"]
        for name in shown:
            lines.append(f"| {name} | {_cell_r(splits[name][part])} |")
        lines.append(f"| adaptive study (cited) | {_cell_r(ADAPTIVE_REF[part])} |")
        return "\n".join(lines)

    verdict = _beats(splits[preset]["oos"], splits["1h/15m baseline"]["oos"])
    if verdict.startswith("the pre-registered variant beat"):
        recommendation = "Optional dry-run experiment only. Do not turn it on for a live account from this one holdout."
    else:
        recommendation = "Do not install, including in dry run. Leave `strategy_variant: default`."
    buffer_note = ("The $0.05 and 0.05% buffers produced the same fills and the same net R on every "
                   "capped row, so the tables show the 0.05% buffer only.") if identical else (
                   "The $0.05 and 0.05% buffers are both shown. They are close, and they are not the same trades.")
    section = f"""<!-- FVG_RR_CAP_BEGIN -->
## Capped take-profit

Same entries, stops, data, fees, and caps as the study above. The only change is the target: the closer of a fixed R and the nearest unmitigated 1h/4h swing or opposing gap. The higher-timeframe level is a cap and is never aimed past. If that capped target is under 1R the trade is dropped in the "min 1R" rows; the "no R filter" rows keep it. 1.5R is a reference. The pre-registered row, chosen before this run, is **{preset}**.

{buffer_note}

### Full 90 days

{table("full")}

### In sample (first 60 days)

{table("is")}

### Out of sample (last 30 days)

{table("oos")}

### Capped-target verdict

{verdict}.

Every capped row lost money on the last 30 days. The full-window gains on 1h with a 2R or 3R cap are in the first 60 days, and those same rows are negative out of sample. That is not a setting to ship.

**Recommendation: {recommendation}**
<!-- FVG_RR_CAP_END -->
"""
    previous = REPORT.read_text(encoding="utf-8")
    begin, end = "<!-- FVG_RR_CAP_BEGIN -->", "<!-- FVG_RR_CAP_END -->"
    if begin in previous and end in previous:
        previous = previous[:previous.index(begin)] + section + previous[previous.index(end) + len(end):]
    else:
        previous = previous.rstrip() + "\n\n" + section
    REPORT.write_text(previous, encoding="utf-8")
    print(section)
    print(f"appended {REPORT}", flush=True)


if __name__ == "__main__":
    import sys
    if "--rr-cap" in sys.argv:
        run_rr_cap()
    else:
        main()
