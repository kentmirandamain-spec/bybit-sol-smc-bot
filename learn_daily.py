#!/usr/bin/env python3
"""
Once-a-day learning pass.

Pulls a rolling candle cache, replays a small neighborhood of the current
config (at most two settings different), and refits the numpy scorer on the
older data only. The most recent 30 days are the out-of-sample check.

A new config replaces the active one only when dry_run is true, the held-out
window has at least 20 trades, net R after fees is higher, and max drawdown
is not more than 20% worse. Live trading writes a proposal and waits.
"""
from __future__ import annotations

import copy
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

import learn
import risk
from strategy import run_strategy

ROOT = Path(__file__).resolve().parent
OOS_DAYS = 30
CACHE_DAYS = 200
MIN_OOS = 20
DD_SLACK = 1.20
TASK_NAME = "GrokBotDailyLearning"

# name -> (config section, key, allowed values)
GRID = {
    "require_choch": ("strategy", "require_choch", (True, False)),
    "use_htf_bias": ("strategy", "use_htf_bias", (True, False)),
    "entry_mode": ("strategy", "entry_mode", ("ob", "fvg", "ob_or_fvg")),
    "sweep_lookback": ("strategy", "sweep_lookback", (50, 100, 150)),
    "rr": ("strategy", "rr", (1.5, 2.0, 2.5, 3.0)),
    "min_score": ("learning", "min_score", (0, 55, 65)),
    "session_skip": ("learning", "session_skip", ("", "Asia", "London", "NY", "Off")),
}


def learning_dir(root: Path) -> Path:
    return Path(root) / "state" / "learning"


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def get_param(cfg: dict, name: str):
    section, key, _values = GRID[name]
    return (cfg.get(section) or {}).get(key, "" if name == "session_skip" else None)


def set_param(cfg: dict, name: str, value) -> None:
    section, key, _values = GRID[name]
    cfg.setdefault(section, {})[key] = value


def changed_params(base: dict, other: dict) -> list[str]:
    return [name for name in GRID if not _same(get_param(base, name), get_param(other, name))]


def single_neighbors(cfg: dict) -> list[dict]:
    """Every config that differs in exactly one searched setting."""
    out = []
    for name, (_section, _key, values) in GRID.items():
        current = get_param(cfg, name)
        for value in values:
            if _same(current, value):
                continue
            nxt = copy.deepcopy(cfg)
            set_param(nxt, name, value)
            out.append(nxt)
    return out


def pair_neighbors(cfg: dict, singles: list[dict], limit: int = 6) -> list[dict]:
    """Pairs of the single-step changes, still at most two settings away from cfg."""
    if limit <= 0:
        return []
    deltas = []
    for other in singles:
        names = changed_params(cfg, other)
        if len(names) == 1:
            deltas.append((names[0], get_param(other, names[0])))
    out = []
    for i in range(len(deltas)):
        for j in range(i + 1, len(deltas)):
            n1, v1 = deltas[i]
            n2, v2 = deltas[j]
            if n1 == n2:
                continue
            nxt = copy.deepcopy(cfg)
            set_param(nxt, n1, v1)
            set_param(nxt, n2, v2)
            out.append(nxt)
            if len(out) >= limit:
                return out
    return out


def beats(candidate: dict, baseline: dict) -> tuple[bool, str]:
    """True when the candidate is allowed to replace the active config."""
    n = int(candidate.get("n") or 0)
    if n < MIN_OOS:
        return False, f"only {n} out-of-sample trades (need {MIN_OOS})"
    base_r = baseline.get("total_r")
    cand_r = candidate.get("total_r")
    if base_r is None or cand_r is None or cand_r <= base_r:
        return False, "out-of-sample net R did not improve"
    base_dd = baseline.get("max_dd") or 0.0
    cand_dd = candidate.get("max_dd") or 0.0
    if cand_dd > base_dd * DD_SLACK + 1e-9:
        return False, "max drawdown is more than 20% worse"
    return True, "higher out-of-sample net R and drawdown within 20%"


def pick_winner(baseline: dict, trials: list[dict]) -> dict | None:
    """trials items have keys params (list), metrics (summarize dict). At most two params."""
    ok = []
    for trial in trials:
        if len(trial.get("params") or []) > 2:
            continue
        good, _why = beats(trial["metrics"], baseline)
        if good:
            ok.append(trial)
    if not ok:
        return None
    ok.sort(key=lambda t: t["metrics"]["total_r"], reverse=True)
    return ok[0]


def _oos_start(now_ms: int, days: int = OOS_DAYS) -> int:
    return int(now_ms) - days * 24 * 3600 * 1000


def _metrics(rows: list[dict]) -> dict:
    stats = learn.summarize(rows)
    equity = []
    total = 0.0
    for row in rows:
        total += float(row["r_net"])
        equity.append(round(total, 4))
    stats["equity"] = equity
    return stats


def _apply_score_and_session(rows: list[dict], model, min_score: float, session_skip: str) -> list[dict]:
    kept = []
    skip = (session_skip or "").strip()
    for row in rows:
        if skip and row.get("session") == skip:
            continue
        if model and min_score and min_score > 0:
            if learn.score_row(model, row) < min_score:
                continue
        kept.append(row)
    return kept


def replay(ltf, htf, cfg: dict) -> list[dict]:
    rules = risk.InstrumentRules()
    setups = run_strategy(ltf, htf, cfg, rules.tick_size)
    rows = learn.rows_from_setups(setups, cfg, source="replay")
    return [r for r in rows if r.get("status") in ("win", "loss") and r.get("tradable") and r.get("r_net") is not None]


def evaluate_config(ltf, htf, cfg: dict, oos_start: int, journal_rows: list[dict]) -> dict:
    rows = replay(ltf, htf, cfg)
    ins = [r for r in rows if (r.get("created_ts") or 0) < oos_start]
    oos = [r for r in rows if (r.get("created_ts") or 0) >= oos_start]
    extra = [r for r in journal_rows if (r.get("created_ts") or 0) < oos_start]
    train = ins + extra
    min_score = float(get_param(cfg, "min_score") or 0)
    session_skip = str(get_param(cfg, "session_skip") or "")
    model = learn.train_model(train, l2=float((cfg.get("learning") or {}).get("l2", 1.0))) if min_score else None
    if min_score and model is None:
        return {"n": 0, "total_r": None, "max_dd": None, "win_rate": None, "expectancy": None,
                "equity": [], "model": None, "note": "not enough in-sample trades to fit a score"}
    kept = _apply_score_and_session(oos, model, min_score, session_skip)
    stats = _metrics(kept)
    stats["model"] = learn._model_public(model) if model else None
    return stats


def describe_changes(base: dict, other: dict) -> list[dict]:
    out = []
    for name in changed_params(base, other):
        out.append({"param": name, "from": get_param(base, name), "to": get_param(other, name)})
    return out


# --------------------------------------------------------------------------- #
# candle cache
# --------------------------------------------------------------------------- #
def _read_cache(path: Path) -> pd.DataFrame | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    df = pd.read_csv(path)
    if df.empty:
        return None
    return df.astype({"ts": "int64", "open": float, "high": float, "low": float, "close": float, "volume": float})


def _write_cache(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.sort_values("ts").drop_duplicates("ts").to_csv(path, index=False)


def update_cache(root: Path, cfg: dict, days: int = CACHE_DAYS, fetch=None, now_ms: int | None = None):
    """Append new candles and keep a rolling window. `fetch(interval, start, end) -> DataFrame`."""
    now_ms = int(now_ms or time.time() * 1000)
    start = now_ms - days * 24 * 3600 * 1000
    folder = learning_dir(root) / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    meta_path = folder / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
    symbol = cfg["symbol"]
    ltf_m, htf_m = int(cfg["ltf_interval"]), int(cfg["htf_interval"])

    def load_one(name, interval):
        path = folder / f"{name}.csv"
        existing = _read_cache(path)
        fetch_start = start
        if existing is not None and len(existing):
            fetch_start = max(start, int(existing["ts"].iloc[-1]) - 2 * interval * 60_000)
        fresh = fetch(interval, fetch_start, now_ms)
        frames = [f for f in (existing, fresh) if f is not None and len(f)]
        if not frames:
            raise RuntimeError(f"no {interval}m candles")
        merged = pd.concat(frames, ignore_index=True)
        merged = merged[merged["ts"] >= start].drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
        _write_cache(path, merged)
        return merged

    if fetch is None:
        source_used = {}

        def fetch(interval, start_ms, end_ms, _used=source_used):
            df, src = _download_interval(symbol, interval, start_ms, end_ms)
            _used[interval] = src
            return df

        ltf = load_one("ltf", ltf_m)
        htf = load_one("htf", htf_m)
        meta.update(source=source_used.get(ltf_m, "bybit"), updated=now_ms, symbol=symbol,
                    ltf_interval=ltf_m, htf_interval=htf_m, rows=int(len(ltf)))
    else:
        ltf = load_one("ltf", ltf_m)
        htf = load_one("htf", htf_m)
        meta.update(source="injected", updated=now_ms, symbol=symbol, rows=int(len(ltf)))
    meta_path.write_text(json.dumps(meta) + "\n", encoding="utf-8")
    return ltf, htf, meta


def _download_interval(symbol: str, interval: int, start_ms: int, end_ms: int):
    import market_data
    errors = []
    for src in ("bybit", "okx"):
        try:
            if src == "bybit":
                df = market_data.bybit_klines_range(symbol, interval, start_ms, end_ms)
            else:
                df = market_data.okx_klines_range(symbol, interval, start_ms, end_ms)
            if len(df) > 50:
                return df, src
            errors.append(f"{src}: {len(df)} rows")
        except Exception as exc:
            errors.append(f"{src}: {exc}")
    raise RuntimeError("candle download failed: " + " | ".join(errors))


# --------------------------------------------------------------------------- #
# config files, log, proposal
# --------------------------------------------------------------------------- #
def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.4f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def patch_config_text(text: str, changes: list[dict]) -> str:
    """Replace the changed keys in place so the commented config keeps its notes."""
    import re
    for change in changes:
        name = change["param"]
        section, key, _values = GRID[name]
        value = _yaml_scalar(change["to"])
        pattern = re.compile(rf"^(\s*{re.escape(key)}:\s*)([^#\n]*?)(\s*#.*)?$", re.M)
        if not pattern.search(text):
            raise ValueError(f"config.yaml has no {section}.{key} line")
        text = pattern.sub(lambda m, value=value: f"{m.group(1)}{value}{m.group(3) or ''}", text, count=1)
        if name == "min_score" and float(change["to"] or 0) > 0:
            text = re.sub(r"^(\s*score_active:\s*)(\S+)", r"\1true", text, count=1, flags=re.M)
            text = re.sub(r"^(\s*enabled:\s*)(\S+)", r"\1true", text, count=1, flags=re.M)
        if name == "session_skip" and str(change["to"] or "").strip():
            text = re.sub(r"^(\s*enabled:\s*)(\S+)", r"\1true", text, count=1, flags=re.M)
    return text


def _append_log(root: Path, entry: dict) -> None:
    folder = learning_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "learning_log.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=learn._py) + "\n")


def _save_version(root: Path, text: str, when: datetime) -> Path:
    folder = learning_dir(root) / "configs"
    folder.mkdir(parents=True, exist_ok=True)
    name = when.strftime("config_%Y%m%d.yaml")
    path = folder / name
    if path.exists():
        path = folder / when.strftime("config_%Y%m%d_%H%M%S.yaml")
    path.write_text(text, encoding="utf-8")
    return path


def _lock(root: Path):
    folder = learning_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "run.lock"
    if path.exists():
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 99999
        if age < 2 * 3600:
            raise RuntimeError("A daily learning run is already in progress.")
    path.write_text(str(os.getpid()), encoding="utf-8")
    return path


def due(root: Path, hours: float = 24) -> bool:
    path = learning_dir(root) / "learning_log.jsonl"
    if not path.exists():
        return True
    last = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            last = line
    if not last:
        return True
    try:
        ts = json.loads(last).get("finished_ts")
    except json.JSONDecodeError:
        return True
    if not ts:
        return True
    return time.time() - float(ts) > hours * 3600


def run(root: Path | None = None, cfg: dict | None = None, frames=None, now_ms: int | None = None,
        on_progress=None, pair_limit: int = 6) -> dict:
    root = Path(root or ROOT)
    cfg_path = root / "config.yaml"
    if cfg is None:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    cfg = copy.deepcopy(cfg)
    now_ms = int(now_ms or time.time() * 1000)
    oos_start = _oos_start(now_ms)

    def progress(msg: str):
        if on_progress:
            on_progress(msg)

    lock = _lock(root)
    try:
        progress("Updating the candle cache…")
        if frames is None:
            ltf, htf, meta = update_cache(root, cfg, now_ms=now_ms)
        else:
            ltf, htf = frames
            meta = {"source": "provided"}
        progress(f"Cache has {len(ltf)} entry candles ({meta.get('source')}). Replaying the current config…")
        journal = [r for r in learn.load_rows(root) if r.get("source") in ("dry_run", "live")]
        base_stats = evaluate_config(ltf, htf, cfg, oos_start, journal)
        base_public = {k: base_stats.get(k) for k in ("n", "wins", "win_rate", "expectancy", "total_r", "max_dd")}
        trials = []
        singles = single_neighbors(cfg)
        # Score the singles first, then only pair the few that are closest to useful.
        ranked = []
        for i, cand in enumerate(singles, start=1):
            names = changed_params(cfg, cand)
            progress(f"Single change {i}/{len(singles)}: {names[0]} -> {get_param(cand, names[0])}")
            stats = evaluate_config(ltf, htf, cand, oos_start, journal)
            trial = {"params": describe_changes(cfg, cand), "metrics": stats, "cfg": cand}
            trials.append(trial)
            ranked.append(trial)
        ranked.sort(key=lambda t: (t["metrics"].get("total_r") is not None, t["metrics"].get("total_r") or -1e9), reverse=True)
        top = [t["cfg"] for t in ranked[:4]]
        for i, cand in enumerate(pair_neighbors(cfg, top, limit=pair_limit), start=1):
            names = changed_params(cfg, cand)
            progress(f"Two-setting change {i}: {', '.join(names)}")
            stats = evaluate_config(ltf, htf, cand, oos_start, journal)
            trials.append({"params": describe_changes(cfg, cand), "metrics": stats, "cfg": cand})
        winner = pick_winner(base_public, [
            {"params": t["params"], "metrics": {k: t["metrics"].get(k) for k in ("n", "total_r", "max_dd", "win_rate", "expectancy")}}
            for t in trials
        ])
        chosen = None
        if winner:
            for trial in trials:
                if trial["params"] == winner["params"]:
                    chosen = trial
                    break
        dry = cfg.get("dry_run") is True
        when = datetime.now(timezone.utc)
        entry = {
            "date": when.strftime("%Y-%m-%d"),
            "finished_ts": time.time(),
            "source": meta.get("source"),
            "oos_start": oos_start,
            "baseline": base_public,
            "changes": chosen["params"] if chosen else [],
            "oos": {k: chosen["metrics"].get(k) for k in ("n", "wins", "win_rate", "expectancy", "total_r", "max_dd")} if chosen else base_public,
            "oos_equity": (chosen or {"metrics": base_stats})["metrics"].get("equity") or [],
            "decision": "keep",
            "reason": "No neighbor beat the current config on the last 30 days.",
        }
        if chosen is None:
            _append_log(root, entry)
            _write_status(root, entry, None)
            progress(entry["reason"])
            return entry
        reason_ok, why = beats(entry["oos"], base_public)
        entry["reason"] = why
        if not reason_ok:
            entry["decision"] = "keep"
            _append_log(root, entry)
            _write_status(root, entry, None)
            return entry
        if not dry:
            proposal = {
                "status": "pending", "kind": "daily", "created": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "changes": chosen["params"], "baseline": base_public, "oos": entry["oos"],
                "reason": why + " Live trading does not apply this on its own.",
                "config_text": patch_config_text(cfg_path.read_text(encoding="utf-8"), chosen["params"]),
                "model": chosen["metrics"].get("model"),
            }
            _write_proposal(root, proposal)
            entry["decision"] = "proposal"
            entry["reason"] = proposal["reason"]
            _append_log(root, entry)
            _write_status(root, entry, proposal)
            progress(entry["reason"])
            return entry
        current_text = cfg_path.read_text(encoding="utf-8")
        text = patch_config_text(current_text, chosen["params"])
        previous = _save_version(root, current_text, when)
        version = _save_version(root, text, when)
        cfg_path.write_text(text, encoding="utf-8")
        if chosen["metrics"].get("model"):
            (learn.journal_dir(root)).mkdir(parents=True, exist_ok=True)
            (learn.journal_dir(root) / "model.json").write_text(
                json.dumps(chosen["metrics"]["model"]) + "\n", encoding="utf-8")
        entry["decision"] = "apply"
        entry["reason"] = why
        entry["saved_config"] = version.name
        entry["previous_config"] = previous.name
        _append_log(root, entry)
        _write_proposal(root, {"status": "applied", "kind": "daily", "changes": chosen["params"], "reason": why})
        _write_status(root, entry, None)
        progress(f"Applied {version.name}: {why}")
        return entry
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _write_proposal(root: Path, proposal: dict) -> None:
    folder = learning_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "proposal.json").write_text(json.dumps(proposal, indent=2, default=learn._py) + "\n", encoding="utf-8")


def _write_status(root: Path, entry: dict, proposal: dict | None) -> None:
    folder = learning_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"last": {k: v for k, v in entry.items() if k != "oos_equity"}, "equity": entry.get("oos_equity") or []}
    (folder / "last_run.json").write_text(json.dumps(payload) + "\n", encoding="utf-8")
    if proposal is None and entry.get("decision") == "keep":
        # Leave an older pending proposal alone only when this run did not decide.
        path = folder / "proposal.json"
        if path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                current = None
            if current and current.get("status") == "pending":
                return
        path.write_text(json.dumps({"status": "none", "reason": entry.get("reason")}) + "\n", encoding="utf-8")


def apply_pending(root: Path) -> dict:
    path = learning_dir(root) / "proposal.json"
    if not path.exists():
        raise ValueError("There is no daily learning proposal.")
    proposal = json.loads(path.read_text(encoding="utf-8"))
    if proposal.get("status") != "pending" or not proposal.get("config_text"):
        raise ValueError("There is no daily proposal waiting to be applied.")
    cfg_path = Path(root) / "config.yaml"
    when = datetime.now(timezone.utc)
    current = cfg_path.read_text(encoding="utf-8")
    previous = _save_version(root, current, when)
    version = _save_version(root, proposal["config_text"], when)
    cfg_path.write_text(proposal["config_text"], encoding="utf-8")
    if proposal.get("model"):
        learn.journal_dir(root).mkdir(parents=True, exist_ok=True)
        (learn.journal_dir(root) / "model.json").write_text(json.dumps(proposal["model"]) + "\n", encoding="utf-8")
    proposal["status"] = "applied"
    proposal["saved_config"] = version.name
    proposal["previous_config"] = previous.name
    _write_proposal(root, proposal)
    _append_log(root, {
        "date": when.strftime("%Y-%m-%d"), "finished_ts": time.time(), "decision": "apply",
        "changes": proposal.get("changes") or [], "reason": "Applied from the dashboard.",
        "oos": proposal.get("oos"), "baseline": proposal.get("baseline"), "saved_config": version.name,
    })
    return proposal


def reject_pending(root: Path) -> dict:
    path = learning_dir(root) / "proposal.json"
    if not path.exists():
        raise ValueError("There is no daily learning proposal.")
    proposal = json.loads(path.read_text(encoding="utf-8"))
    if proposal.get("status") != "pending":
        raise ValueError("There is no daily proposal waiting.")
    proposal["status"] = "rejected"
    _write_proposal(root, proposal)
    return proposal


def list_configs(root: Path) -> list[dict]:
    folder = learning_dir(root) / "configs"
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("config_*.yaml"), reverse=True):
        out.append({"file": path.name, "bytes": path.stat().st_size})
    return out


def rollback_config(root: Path, filename: str) -> dict:
    if not filename or "/" in filename or "\\" in filename or not filename.startswith("config_") or not filename.endswith(".yaml"):
        raise ValueError("Unknown config snapshot.")
    path = learning_dir(root) / "configs" / filename
    if not path.is_file():
        raise ValueError("That config snapshot is not on disk.")
    text = path.read_text(encoding="utf-8")
    cfg_path = Path(root) / "config.yaml"
    when = datetime.now(timezone.utc)
    saved = _save_version(root, cfg_path.read_text(encoding="utf-8"), when)
    cfg_path.write_text(text, encoding="utf-8")
    _append_log(root, {
        "date": when.strftime("%Y-%m-%d"), "finished_ts": time.time(), "decision": "rollback",
        "changes": [], "reason": f"Restored {filename}. The replaced file was saved as {saved.name}.",
        "saved_config": filename,
    })
    return {"restored": filename, "saved_current_as": saved.name}


def read_log(root: Path, limit: int = 30) -> list[dict]:
    path = learning_dir(root) / "learning_log.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def status(root: Path) -> dict:
    log_rows = read_log(root, 30)
    last = log_rows[-1] if log_rows else None
    equity = []
    last_path = learning_dir(root) / "last_run.json"
    if last_path.exists():
        try:
            equity = json.loads(last_path.read_text(encoding="utf-8")).get("equity") or []
        except json.JSONDecodeError:
            equity = []
    proposal = None
    prop_path = learning_dir(root) / "proposal.json"
    if prop_path.exists():
        try:
            proposal = json.loads(prop_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            proposal = None
    previous = log_rows[-2] if len(log_rows) > 1 else None
    return {
        "last_run": None if not last else last.get("finished_ts"),
        "last_decision": None if not last else last.get("decision"),
        "last_reason": None if not last else last.get("reason"),
        "next_run": "00:15 local time, or when the dashboard starts if the last run is more than 24 hours old",
        "due": due(root),
        "baseline": None if not last else last.get("baseline"),
        "oos": None if not last else last.get("oos"),
        "previous": None if not previous else {"decision": previous.get("decision"), "oos": previous.get("oos"),
                                                "baseline": previous.get("baseline"), "reason": previous.get("reason")},
        "changes": None if not last else last.get("changes"),
        "history": [{k: row.get(k) for k in ("date", "decision", "reason", "changes", "oos", "baseline")} for row in log_rows],
        "oos_equity": equity,
        "configs": list_configs(root),
        "proposal": proposal,
    }


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Daily dry-run learning pass")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    args = ap.parse_args()
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    result = run(ROOT, cfg, on_progress=print)
    print(json.dumps({k: result.get(k) for k in ("decision", "reason", "changes", "baseline", "oos")}, indent=2, default=str))


if __name__ == "__main__":
    main()
