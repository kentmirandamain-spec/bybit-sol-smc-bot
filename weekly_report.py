"""Sunday 20:00 Asia/Manila weekly note. Manila is UTC+8, so that is 12:00 UTC."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


MANILA = timezone(timedelta(hours=8))


def week_id(when: datetime) -> str:
    local = when.astimezone(MANILA)
    year, week, _day = local.isocalendar()
    return f"{year}-W{week:02d}"


def due(now: datetime, last_week: str | None) -> bool:
    local = now.astimezone(MANILA)
    if local.weekday() != 6 or local.hour < 20:
        return False
    return last_week != week_id(now)


def build(trades: list[dict], learning: list[dict] | None = None, when: datetime | None = None) -> dict:
    when = when or datetime.now(timezone.utc)
    start = when - timedelta(days=7)
    rows = []
    for trade in trades:
        stamp = trade.get("closed_at") or trade.get("created_at") or ""
        try:
            ts = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts >= start:
            rows.append(trade)
    closed = [t for t in rows if t.get("r") is not None and t.get("status") in ("win", "loss", "closed")]
    rs = [float(t["r"]) for t in closed]
    wins = sum(1 for r in rs if r > 0)
    n = len(rs)
    best = max(closed, key=lambda t: float(t["r"])) if closed else None
    worst = min(closed, key=lambda t: float(t["r"])) if closed else None
    eq = 0.0
    peak = 0.0
    dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {
        "week": week_id(when),
        "generated": when.astimezone(MANILA).strftime("%Y-%m-%d %H:%M Asia/Manila"),
        "trades": n,
        "win_rate": None if not n else round(wins / n, 4),
        "expectancy": None if not n else round(sum(rs) / n, 4),
        "total_r": round(sum(rs), 4) if rs else 0,
        "max_dd": round(dd, 4),
        "best": None if not best else {"symbol": best.get("symbol"), "r": best.get("r"), "side": best.get("side")},
        "worst": None if not worst else {"symbol": worst.get("symbol"), "r": worst.get("r"), "side": worst.get("side")},
        "learning": learning or [],
        "note": "A quiet week is not a signal to loosen risk. Risk stays at most 5 USDT a trade.",
    }


def write(root: Path, report: dict) -> Path:
    folder = Path(root) / "state" / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"weekly_{report['week']}.md"
    wr = "n/a" if report["win_rate"] is None else f"{100 * report['win_rate']:.1f}%"
    lines = [
        f"# Week {report['week']}",
        "",
        f"Generated {report['generated']}.",
        "",
        f"- Trades: {report['trades']}",
        f"- Win rate: {wr}",
        f"- Expectancy: {report['expectancy']} R",
        f"- Total: {report['total_r']} R",
        f"- Max drawdown: {report['max_dd']} R",
        f"- Best: {report['best']}",
        f"- Worst: {report['worst']}",
        "",
        report["note"],
        "",
    ]
    if report["learning"]:
        lines.append("Learning changes:")
        for item in report["learning"]:
            lines.append(f"- {item}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return path
