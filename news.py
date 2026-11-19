"""High-impact USD news blackout. Fails open if the calendar cannot be loaded."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
KEYWORDS = ("CPI", "FOMC", "NFP", "NON-FARM", "NONFARM", "PCE", "GDP", "FOMC")


def parse_events(payload, pause_minutes: int) -> list[tuple[datetime, datetime, str]]:
    events = payload if isinstance(payload, list) else []
    windows = []
    pause = timedelta(minutes=max(0, int(pause_minutes)))
    for event in events:
        if str(event.get("country") or event.get("currency") or "").upper() not in ("USD", "US"):
            continue
        impact = str(event.get("impact") or "").lower()
        title = str(event.get("title") or "")
        if impact != "high" and not any(word in title.upper() for word in KEYWORDS):
            continue
        when = _parse_when(event.get("date") or event.get("time"))
        if when is None:
            continue
        windows.append((when - pause, when + pause, title or "USD high impact"))
    return windows


def parse_manual(text: str, pause_minutes: int) -> list[tuple[datetime, datetime, str]]:
    windows = []
    pause = timedelta(minutes=max(0, int(pause_minutes)))
    for piece in str(text or "").split(","):
        piece = piece.strip()
        if not piece:
            continue
        when = _parse_when(piece)
        if when is None:
            continue
        windows.append((when - pause, when + pause, "manual blackout"))
    return windows


def _parse_when(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%m-%d-%Y %I:%M%p"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def in_blackout(now: datetime, windows) -> str | None:
    for start, end, title in windows:
        if start <= now <= end:
            return title
    return None


def load_cached(path: Path, max_age_s: int = 20 * 3600):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - float(payload.get("fetched") or 0) > max_age_s:
        return None
    return payload.get("events")


def store_cache(path: Path, events) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched": time.time(), "events": events}) + "\n", encoding="utf-8")


def fetch_events(url: str = FEED_URL, timeout: float = 10):
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def upcoming(root: Path, cfg: dict, now: datetime | None = None, fetch=None) -> dict:
    """Return windows and whether new entries should wait. Unreachable feed fails open."""
    now = now or datetime.now(timezone.utc)
    minutes = int((cfg.get("news_pause_minutes") or 30))
    manual = parse_manual(cfg.get("news_blackouts") or "", minutes)
    warning = ""
    events = []
    if cfg.get("news_filter"):
        cache = Path(root) / "state" / "news" / "calendar.json"
        events = load_cached(cache)
        if events is None and fetch is not False:
            try:
                events = fetch() if fetch else fetch_events()
                store_cache(cache, events)
            except Exception as exc:
                events = []
                warning = f"News calendar unreachable ({exc}). New entries are allowed."
        events = events or []
    windows = manual + (parse_events(events, minutes) if cfg.get("news_filter") else [])
    hit = in_blackout(now, windows)
    view = [{
        "start": a.strftime("%Y-%m-%d %H:%M UTC"),
        "end": b.strftime("%Y-%m-%d %H:%M UTC"),
        "title": title,
    } for a, b, title in windows]
    return {"blocked": bool(hit), "title": hit or "", "windows": view[:12], "warning": warning}
