#!/usr/bin/env python3
"""
Local web dashboard for the Bybit SOLUSDT SMC bot.

  python dashboard.py                 # http://127.0.0.1:8080
  python dashboard.py --port 8090 --no-browser

Pure standard library (http.server) + the bot's existing deps - no extra installs.
Binds to 127.0.0.1 only. Optional password: DASHBOARD_PASSWORD=... in .env
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import yaml
from dotenv import load_dotenv

import learn
import learn_daily
import updater

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
STATE = ROOT / "state"
CMD_DIR = STATE / "commands"
STATUS_FILE = STATE / "status.json"
CANDLES_FILE = STATE / "candles.json"
TRADES_FILE = STATE / "trades.json"
LOG_FILE = ROOT / "logs" / "bot.log"
CONSOLE_LOG = ROOT / "logs" / "bot_console.log"
PY = sys.executable
WIN = os.name == "nt"
NOWIN = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if WIN else 0

load_dotenv(ROOT / ".env")
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()
SESSIONS: set[str] = set()
LOCK = threading.Lock()
BOT_PROC: subprocess.Popen | None = None
JOBS: dict[str, dict] = {}
SERVER: ThreadingHTTPServer | None = None
DASH_PORT = int(os.getenv("DASHBOARD_PORT", "8080") or "8080")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def read_json(path: Path, default):
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, PermissionError, OSError):
            time.sleep(0.05)
    return default


def tail(path: Path, lines: int = 200) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            data = f.read().decode("utf-8", "replace")
        return data.splitlines()[-lines:]
    except FileNotFoundError:
        return []


def send_command(cmd: str):
    CMD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}_{cmd}.json"
    tmp = CMD_DIR / (name + ".tmp")
    tmp.write_text(json.dumps({"command": cmd, "at": time.time()}), encoding="utf-8")
    os.replace(tmp, CMD_DIR / name)


def bot_state() -> dict:
    st = read_json(STATUS_FILE, {})
    managed = BOT_PROC is not None and BOT_PROC.poll() is None
    hb_age = time.time() - float(st.get("heartbeat_epoch") or 0)
    alive = managed or (st.get("running") and hb_age < 20)
    st["alive"] = bool(alive)
    st["managed"] = managed
    st["heartbeat_age_s"] = round(hb_age, 1) if st.get("heartbeat_epoch") else None
    if BOT_PROC is not None and BOT_PROC.poll() is not None:
        st["exit_code"] = BOT_PROC.returncode
    return st


def trade_stats(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("status") in ("win", "loss", "closed") and t.get("r") is not None]
    wins = sum(1 for t in closed if (t.get("pnl_usdt") or 0) > 0)
    return dict(total=len(trades), closed=len(closed), wins=wins, losses=len(closed) - wins,
                win_rate=round(100 * wins / len(closed), 1) if closed else None,
                total_r=round(sum(t["r"] for t in closed), 2),
                total_pnl=round(sum(t.get("pnl_usdt") or 0 for t in closed), 2))


CANDLE_CACHE: dict = {}


def get_candles(source: str, limit: int = 200) -> dict:
    """Recent LTF candles for the chart: the bot's own file if fresh, else a public fetch (cached 60 s)."""
    cfg = load_cfg()
    interval, symbol = int(cfg["ltf_interval"]), cfg["symbol"]
    d = read_json(CANDLES_FILE, None)
    if d and d.get("candles") and d.get("interval") == interval and time.time() - float(d.get("written", 0)) < 1800:
        return {**d, "candles": d["candles"][-limit:], "from": "bot"}
    key = (source, interval)
    c = CANDLE_CACHE.get(key)
    if c and time.time() - c["written"] < 60:
        return {**c, "candles": c["candles"][-limit:]}
    try:
        import market_data   # imported lazily (pulls pandas)
        if source == "okx":
            df = market_data.okx_klines(symbol, interval, 300)
        else:
            end = int(time.time() * 1000)
            df = market_data.bybit_klines_range(symbol, interval, end - 300 * interval * 60_000, end)
        c = {"written": time.time(), "symbol": symbol, "interval": interval, "source": source, "from": "public",
             "candles": [[int(r.ts), r.open, r.high, r.low, r.close, r.volume] for r in df.tail(300).itertuples()]}
        CANDLE_CACHE[key] = c
        return {**c, "candles": c["candles"][-limit:]}
    except Exception as e:
        msg = str(e).splitlines()[0][:200]
        return {"candles": [], "interval": interval, "symbol": symbol, "source": source,
                "error": f"Could not load {source} candles: {msg}"}


# --------------------------------------------------------------------------- #
# settings (edits config.yaml line-by-line to keep the comments)
# --------------------------------------------------------------------------- #
INTERVALS = [1, 3, 5, 15, 30, 60, 120, 240, 360, 720, 1440]
FIELDS = {   # key: (section, type, validator-description)
    "risk_usdt": ("risk", float, (0.5, 1000)),
    "leverage": ("risk", int, (1, 50)),
    "max_daily_loss_usdt": ("risk", float, (0, 100000)),
    "max_trades_per_day": ("risk", int, (0, 100)),
    "rr": ("strategy", float, (0.5, 10)),
    "require_choch": ("strategy", bool, None),
    "use_htf_bias": ("strategy", bool, None),
    "entry_mode": ("strategy", str, ["ob", "fvg", "ob_or_fvg"]),
    "tp_mode": ("strategy", str, ["rr", "liquidity"]),
    "htf_interval": (None, int, INTERVALS),
    "ltf_interval": (None, int, INTERVALS),
    "dry_run": (None, bool, None),
    "testnet": (None, bool, None),
}


def load_cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def current_settings(cfg=None) -> dict:
    cfg = cfg or load_cfg()
    out = {}
    for k, (sec, _, _) in FIELDS.items():
        out[k] = (cfg.get(sec) or {}).get(k) if sec else cfg.get(k)
    env_over = {k: os.getenv(e) for k, e in (("dry_run", "BOT_DRY_RUN"), ("testnet", "BOT_TESTNET")) if os.getenv(e)}
    out["_env_overrides"] = env_over
    return out


def _parse(key, val):
    _, typ, rule = FIELDS[key]
    if typ is bool:
        if isinstance(val, bool):
            return val
        if str(val).lower() in ("true", "1", "yes", "on"):
            return True
        if str(val).lower() in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{key}: must be true/false")
    try:
        v = typ(val)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: invalid value {val!r}")
    if typ is float and v != v:
        raise ValueError(f"{key}: invalid number")
    if isinstance(rule, tuple) and not (rule[0] <= v <= rule[1]):
        raise ValueError(f"{key}: must be between {rule[0]} and {rule[1]}")
    if isinstance(rule, list) and v not in rule:
        raise ValueError(f"{key}: must be one of {rule}")
    return v


def _yaml_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def save_settings(data: dict) -> dict:
    old = current_settings()
    new = {}
    for k, v in data.items():
        if k in FIELDS:
            pv = _parse(k, v)
            if old[k] is None or pv != old[k]:   # only write fields that actually changed
                new[k] = pv
    merged = {**{k: old[k] for k in FIELDS}, **new}
    if merged["ltf_interval"] >= merged["htf_interval"] or merged["htf_interval"] % merged["ltf_interval"]:
        raise ValueError("Entry timeframe must be smaller than and divide evenly into the bias timeframe")
    was_live = (old["dry_run"] is False and old["testnet"] is False)
    goes_live = (merged["dry_run"] is False and merged["testnet"] is False)
    if goes_live and not was_live and str(data.get("confirm", "")).strip() != "GO LIVE":
        raise ValueError("Switching to LIVE trading needs the typed confirmation: GO LIVE")

    text = CONFIG.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    section = None
    for i, line in enumerate(lines):
        m_sec = re.match(r"^([A-Za-z_]+):\s*(#.*)?$", line.rstrip("\r\n"))
        if m_sec:
            section = m_sec.group(1)
            continue
        if line and not line[0].isspace():
            section = None
        m = re.match(r"^(\s*)([A-Za-z_]+)(:\s*)([^#\r\n]*?)(\s*#.*)?(\r?\n)?$", line)
        if not m:
            continue
        key = m.group(2)
        if key in new:
            want_sec = FIELDS[key][0]
            if (want_sec is None and m.group(1) == "") or (want_sec == section and m.group(1) != ""):
                val = _yaml_scalar(new[key])
                comment = (m.group(5) or "").lstrip()
                head = f"{m.group(1)}{key}{m.group(3)}{val}"
                if comment:   # keep the comment in its original column
                    col = line.find("#")
                    head += " " * max(1, col - len(head))
                lines[i] = f"{head}{comment}{m.group(6) or ''}"
    out = "".join(lines)
    parsed = yaml.safe_load(out)            # validate result before writing
    check = current_settings(parsed)
    for k, v in new.items():
        if check[k] != v:
            raise ValueError(f"could not update {k} in config.yaml")
    backup = CONFIG.with_suffix(".yaml.bak")
    backup.write_text(text, encoding="utf-8")
    tmp = CONFIG.with_suffix(".yaml.tmp")
    tmp.write_text(out, encoding="utf-8")
    os.replace(tmp, CONFIG)
    return check


# --------------------------------------------------------------------------- #
# bot process control
# --------------------------------------------------------------------------- #
def start_bot(data_source: str) -> str:
    global BOT_PROC
    with LOCK:
        if bot_state()["alive"]:
            raise ValueError("Bot is already running")
        args = [PY, "-u", str(ROOT / "bot.py")]
        if data_source in ("bybit", "okx"):
            args += ["--data-source", data_source]
        CONSOLE_LOG.parent.mkdir(parents=True, exist_ok=True)
        out = open(CONSOLE_LOG, "a", encoding="utf-8")
        out.write(f"\n===== bot start {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
        out.flush()
        offset = out.tell()
        env = {**os.environ, "SMCBOT_QUIET": "1", "PYTHONIOENCODING": "utf-8"}
        BOT_PROC = subprocess.Popen(args, cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=NOWIN, env=env)
        out.close()
    time.sleep(2)
    if BOT_PROC.poll() is not None:
        with open(CONSOLE_LOG, "rb") as f:
            f.seek(offset)
            msg = f.read().decode("utf-8", "replace").strip()
        raise ValueError(f"Bot exited immediately (code {BOT_PROC.returncode}): {msg[-600:]}")
    return f"Bot started (pid {BOT_PROC.pid})"


def stop_bot() -> str:
    global BOT_PROC
    st = bot_state()
    if not st["alive"]:
        return "Bot is not running"
    send_command("stop")
    for _ in range(30):
        time.sleep(0.5)
        if BOT_PROC is not None and BOT_PROC.poll() is not None:
            return "Bot stopped"
        if BOT_PROC is None and not read_json(STATUS_FILE, {}).get("running", False):
            return "Bot stopped"
    if BOT_PROC is not None and BOT_PROC.poll() is None:
        BOT_PROC.terminate()
        return "Bot did not stop in 15 s - terminated"
    pid = st.get("pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
            return f"Sent terminate to pid {pid}"
        except OSError as e:
            return f"Could not stop pid {pid}: {e}"
    return "Stop requested"


def _update_busy() -> bool:
    return any(v.get("status") == "running" and v.get("kind") in ("update", "rollback") for v in JOBS.values())


def run_custom_job(kind: str, work) -> str:
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"id": jid, "kind": kind, "status": "running", "output": "", "started": time.time(),
                 "restarting": False, "message": ""}

    def wrap():
        try:
            work(jid)
        except Exception as e:
            JOBS[jid].update(status="error", output=updater.redact(str(e)), message="Update failed")
    threading.Thread(target=wrap, daemon=True).start()
    for old in [k for k, v in JOBS.items() if time.time() - v["started"] > 3600]:
        JOBS.pop(old, None)
    return jid


def schedule_dashboard_restart() -> bool:
    """Open a new dashboard window on Windows, then stop this one so the port is free.

    Other platforms only tell the user to restart: stopping a process that a service
    manager owns is not safe from here.
    """
    if os.name != "nt":
        return False
    if os.getenv("SMCBOT_NO_RESTART", "").strip().lower() in ("1", "true", "yes"):
        return False
    port = DASH_PORT or 8080
    script = str(ROOT / "dashboard.py")
    # ping waits ~4s so this process can release the port. `start` opens a new console.
    cmd = f'ping 127.0.0.1 -n 5 >nul & start "SOL SMC Bot Dashboard" "{PY}" -u "{script}" --port {port}'
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(["cmd.exe", "/c", cmd], cwd=str(ROOT), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)

    def _stop():
        if SERVER is not None:
            SERVER.shutdown()
    threading.Timer(2.0, _stop).start()
    return True


def _finish_update_job(jid: str, result: updater.ApplyResult) -> None:
    restarting = False
    text = result.output
    if result.ok and os.name == "nt":
        restarting = schedule_dashboard_restart()
        if restarting:
            text += "\n\nRestarting the dashboard…"
    JOBS[jid].update(status="done" if result.ok else "error", output=text, restarting=restarting,
                     message=result.summary)


def _run_update(jid: str) -> None:
    def progress(msg: str) -> None:
        JOBS[jid]["output"] = msg
    try:
        result = updater.perform_update(ROOT, token=updater.github_token(ROOT), status_fn=bot_state,
                                        on_progress=progress)
    except Exception as e:
        JOBS[jid].update(status="error", output=updater.redact(str(e)), message="Update failed")
        return
    _finish_update_job(jid, result)


def _run_learn(jid: str, days: int, source: str) -> None:
    def progress(msg: str) -> None:
        JOBS[jid]["output"] = msg
    try:
        text = learn.run_study(ROOT, load_cfg(), days=days, source=source, on_progress=progress)
    except Exception as e:
        JOBS[jid].update(status="error", output=learn_redact(str(e)), message="Learning study failed")
        return
    JOBS[jid].update(status="done", output=text, message="Learning study finished")


def learn_redact(text: str) -> str:
    return text[-4000:]


def _run_rollback(jid: str, backup: str | None) -> None:
    def progress(msg: str) -> None:
        JOBS[jid]["output"] = msg
    try:
        result = updater.rollback(ROOT, backup, status_fn=bot_state, on_progress=progress)
    except Exception as e:
        JOBS[jid].update(status="error", output=updater.redact(str(e)), message="Rollback failed")
        return
    _finish_update_job(jid, result)


def run_job(kind: str, args: list[str], env_extra: dict | None = None) -> str:
    jid = uuid.uuid4().hex[:10]
    JOBS[jid] = {"id": jid, "kind": kind, "status": "running", "output": "", "started": time.time()}

    def work():
        env = {**os.environ, **(env_extra or {}), "PYTHONIOENCODING": "utf-8"}
        try:
            p = subprocess.run([PY, "-u", *args], cwd=str(ROOT), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=900, env=env, creationflags=NOWIN)
            JOBS[jid].update(status="done" if p.returncode == 0 else "error",
                             output=(p.stdout + ("\n" + p.stderr if p.stderr.strip() else "")).strip(),
                             code=p.returncode)
        except subprocess.TimeoutExpired:
            JOBS[jid].update(status="error", output="Timed out after 15 minutes")
        except Exception as e:
            JOBS[jid].update(status="error", output=str(e))
    threading.Thread(target=work, daemon=True).start()
    for old in [k for k, v in JOBS.items() if time.time() - v["started"] > 3600]:
        JOBS.pop(old, None)
    return jid


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "GrokBotDashboard/2.2"

    def log_message(self, fmt, *args):   # keep console quiet
        pass

    # -- utils
    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html, code=200, extra_headers=None):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # X-Frame-Options stops other sites embedding this dashboard.
        # Do not add a Content-Security-Policy here: the Overview chart loads
        # https://s3.tradingview.com/tv.js in the browser and frames tradingview.com.
        # _host_ok only checks requests that reach this process.
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    def _authed(self):
        if not PASSWORD:
            return True
        c = cookies.SimpleCookie(self.headers.get("Cookie") or "")
        tok = c.get("smcdash")
        return bool(tok and tok.value in SESSIONS)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
            return {}
        raw = self.rfile.read(n) if n else b""
        ctype = self.headers.get("Content-Type", "")
        if "application/json" in ctype:
            try:
                return json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return {}
        return {k: v[0] for k, v in parse_qs(raw.decode()).items()}

    # -- GET
    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            if not self._authed():
                return self._html(LOGIN_HTML)
            return self._html(page_html())
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if not self._authed():
            return self._json({"error": "login required"}, 401)
        if path == "/api/status":
            trades = read_json(TRADES_FILE, [])
            cfg = current_settings()
            return self._json({"bot": bot_state(), "settings": cfg, "stats": trade_stats(trades),
                               "stats_by_mode": {m: trade_stats([t for t in trades if t.get("mode") == m])
                                                 for m in ("DRY_RUN", "TESTNET", "LIVE")},
                               "trades": list(reversed(trades[-200:])), "server_time": time.time()})
        if path == "/api/log":
            n = int(parse_qs(urlparse(self.path).query).get("n", ["150"])[0])
            return self._json({"lines": tail(LOG_FILE, min(max(n, 10), 1000)),
                               "console": tail(CONSOLE_LOG, 20)})
        if path == "/api/settings":
            return self._json(current_settings())
        if path == "/api/candles":
            q = parse_qs(urlparse(self.path).query)
            src = q.get("source", ["bybit"])[0]
            src = src if src in ("bybit", "okx") else "bybit"
            lim = min(max(int(q.get("limit", ["200"])[0]), 20), 300)
            return self._json(get_candles(src, lim))
        if path == "/api/job":
            jid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            j = JOBS.get(jid)
            return self._json(j if j else {"error": "unknown job"}, 200 if j else 404)
        if path == "/api/learning":
            view = learn.dashboard_view(ROOT, load_cfg())
            view["daily"] = learn_daily.status(ROOT)
            return self._json(view)
        if path == "/api/about":
            meta = updater.read_installed(ROOT)
            commit = meta.get("commit") if isinstance(meta.get("commit"), str) else None
            return self._json({
                "version": updater.read_version(ROOT),
                "commit": commit,
                "updated_at": meta.get("updated_at"),
                "backups": updater.list_backups(ROOT),
                "blocked": updater.trading_block_reason(bot_state()),
                "auto_restart": os.name == "nt",
            })
        return self._json({"error": "not found"}, 404)

    # -- POST
    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        path = urlparse(self.path).path
        if path == "/login":
            data = self._body()
            if PASSWORD and hmac.compare_digest(str(data.get("password", "")).encode(), PASSWORD.encode()):
                tok = secrets.token_urlsafe(24)
                SESSIONS.add(tok)
                self.send_response(303)
                self.send_header("Set-Cookie", f"smcdash={tok}; HttpOnly; SameSite=Strict; Path=/")
                self.send_header("Location", "/")
                self.end_headers()
                return
            return self._html(LOGIN_HTML.replace("<!--ERR-->", "<p class=err>Wrong password</p>"), 401)
        # CSRF guard: custom header can't be sent cross-site without a CORS preflight we never allow
        if self.headers.get("X-Dashboard") != "1":
            return self._json({"error": "missing X-Dashboard header"}, 403)
        if not self._authed():
            return self._json({"error": "login required"}, 401)
        data = self._body()
        try:
            if path == "/api/bot/start":
                return self._json({"ok": True, "message": start_bot(data.get("data_source", "bybit"))})
            if path == "/api/bot/stop":
                return self._json({"ok": True, "message": stop_bot()})
            if path == "/api/command":
                cmd = data.get("command")
                if cmd not in ("pause", "resume", "cancel_pending", "close_position", "evaluate_now"):
                    raise ValueError("unknown command")
                if cmd == "close_position" and data.get("confirm") is not True:
                    raise ValueError("close_position needs confirm=true")
                if not bot_state()["alive"]:
                    raise ValueError("Bot is not running - start it first")
                send_command(cmd)
                return self._json({"ok": True, "message": f"'{cmd}' sent to bot"})
            if path == "/api/once":
                if bot_state()["alive"]:
                    send_command("evaluate_now")
                    return self._json({"ok": True, "message": "Bot is running - asked it to evaluate now (see log)"})
                args = ["bot.py", "--once"]
                if data.get("data_source") in ("bybit", "okx"):
                    args += ["--data-source", data["data_source"]]
                # one-off checks from the dashboard never place real orders
                jid = run_job("once", args, {"BOT_DRY_RUN": "true"})
                return self._json({"ok": True, "job": jid, "message": "Signal check started (dry run, no orders)"})
            if path == "/api/backtest":
                days = int(data.get("days", 90))
                if not 7 <= days <= 365:
                    raise ValueError("days must be 7-365")
                src = data.get("source", "bybit")
                if src not in ("bybit", "okx"):
                    raise ValueError("source must be bybit or okx")
                jid = run_job("backtest", ["backtest.py", "--days", str(days), "--source", src,
                                           "--csv", "state/backtest_trades.csv"])
                return self._json({"ok": True, "job": jid, "message": f"Backtest {days}d ({src}) started"})
            if path == "/api/settings":
                saved = save_settings(data)
                msg = "Settings saved." + (" Restart the bot to apply them." if bot_state()["alive"] else "")
                return self._json({"ok": True, "message": msg, "settings": saved})
            if path == "/api/update/check":
                info = updater.check_for_updates(ROOT, token=updater.github_token(ROOT))
                info["blocked"] = updater.trading_block_reason(bot_state())
                return self._json(info)
            if path == "/api/update/apply":
                if data.get("confirm") is not True:
                    raise ValueError("Updating needs confirm=true")
                reason = updater.trading_block_reason(bot_state())
                if reason:
                    raise ValueError(reason)
                if _update_busy():
                    raise ValueError("An update or rollback is already running")
                jid = run_custom_job("update", _run_update)
                return self._json({"ok": True, "job": jid, "message": "Update started"})
            if path == "/api/learning/rebuild":
                if data.get("confirm") is not True:
                    raise ValueError("Rebuilding the journal needs confirm=true")
                days = int(data.get("days", 90))
                if not 30 <= days <= 365:
                    raise ValueError("days must be 30-365")
                source = data.get("source", "bybit")
                if source not in ("bybit", "okx"):
                    raise ValueError("source must be bybit or okx")
                if _update_busy() or any(v.get("status") == "running" and v.get("kind") == "learn" for v in JOBS.values()):
                    raise ValueError("A study or update is already running")
                jid = run_custom_job("learn", lambda job_id, d=days, src=source: _run_learn(job_id, d, src))
                return self._json({"ok": True, "job": jid, "message": f"Learning study started ({days} days)"})
            if path == "/api/learning/daily-apply":
                if data.get("confirm") is not True:
                    raise ValueError("Applying the daily proposal needs confirm=true")
                proposal = learn_daily.apply_pending(ROOT)
                return self._json({"ok": True, "message": "Daily proposal applied. The dry-run bot reloads it on the next bar.",
                                   "saved": proposal.get("saved_config")})
            if path == "/api/learning/daily-reject":
                if data.get("confirm") is not True:
                    raise ValueError("Rejecting the daily proposal needs confirm=true")
                learn_daily.reject_pending(ROOT)
                return self._json({"ok": True, "message": "Daily proposal rejected. Config was not changed."})
            if path == "/api/learning/rollback-config":
                if data.get("confirm") is not True:
                    raise ValueError("Restoring a config needs confirm=true")
                result = learn_daily.rollback_config(ROOT, str(data.get("file") or ""))
                return self._json({"ok": True, "message": f"Restored {result['restored']}.", "result": result})
            if path == "/api/learning/apply":
                if data.get("confirm") is not True:
                    raise ValueError("Applying a learning proposal needs confirm=true")
                proposal = learn.apply_proposal(ROOT, CONFIG)
                return self._json({"ok": True, "message": "Proposal applied. Restart the bot so it uses the new rules.",
                                   "proposal": {k: proposal.get(k) for k in ("status", "why", "backup", "min_score")}})
            if path == "/api/learning/reject":
                if data.get("confirm") is not True:
                    raise ValueError("Rejecting a proposal needs confirm=true")
                learn.reject_proposal(ROOT)
                return self._json({"ok": True, "message": "Proposal rejected. Config was not changed."})
            if path == "/api/update/rollback":
                if data.get("confirm") is not True:
                    raise ValueError("Rollback needs confirm=true")
                reason = updater.trading_block_reason(bot_state())
                if reason:
                    raise ValueError(reason)
                if _update_busy():
                    raise ValueError("An update or rollback is already running")
                backup = data.get("backup")
                if backup is not None and not isinstance(backup, str):
                    raise ValueError("backup must be a name")
                jid = run_custom_job("rollback", lambda job_id, name=backup: _run_rollback(job_id, name))
                return self._json({"ok": True, "job": jid, "message": "Rollback started"})
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
        return self._json({"error": "not found"}, 404)


# --------------------------------------------------------------------------- #
LOGIN_HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Grok Bot - Login</title><style>body{font-family:system-ui,Segoe UI,sans-serif;background:#0f172a;color:#e2e8f0;display:flex;
align-items:center;justify-content:center;height:100vh;margin:0}form{background:#1e293b;padding:28px;border-radius:12px;width:280px}
input,button{width:100%;padding:10px;margin-top:10px;border-radius:8px;border:1px solid #334155;background:#0f172a;color:#e2e8f0;box-sizing:border-box}
button{background:#2563eb;border:0;font-weight:600;cursor:pointer}.err{color:#f87171}</style></head><body>
<form method=post action=/login><svg width="36" height="36" viewBox="0 0 512 509.641" aria-hidden="true" style="color:#f4f7fb"><path fill="currentColor" d="M213.235 306.019l178.976-180.002v.169l51.695-51.763c-.924 1.32-1.86 2.605-2.785 3.89-39.281 54.164-58.46 80.649-43.07 146.922l-.09-.101c10.61 45.11-.744 95.137-37.398 131.836-46.216 46.306-120.167 56.611-181.063 14.928l42.462-19.675c38.863 15.278 81.392 8.57 111.947-22.03 30.566-30.6 37.432-75.159 22.065-112.252-2.92-7.025-11.67-8.795-17.792-4.263l-124.947 92.341zm-25.786 22.437l-.033.034L68.094 435.217c7.565-10.429 16.957-20.294 26.327-30.149 26.428-27.803 52.653-55.359 36.654-94.302-21.422-52.112-8.952-113.177 30.724-152.898 41.243-41.254 101.98-51.661 152.706-30.758 11.23 4.172 21.016 10.114 28.638 15.639l-42.359 19.584c-39.44-16.563-84.629-5.299-112.207 22.313-37.298 37.308-44.84 102.003-1.128 143.81z"/></svg><h3>Grok Bot · SOL Futures</h3><!--ERR--><input type=password name=password placeholder="Dashboard password" autofocus>
<button>Log in</button></form></body></html>"""

def page_html() -> str:
    f = ROOT / "dashboard.html"
    html = f.read_text(encoding="utf-8") if f.exists() else "<h1>dashboard.html missing</h1>"
    return html.replace("<!--APP_VERSION-->", updater.read_version(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "8080")))
    ap.add_argument("--no-browser", action="store_true")
    global SERVER, DASH_PORT
    args = ap.parse_args()
    DASH_PORT = args.port
    STATE.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    SERVER = srv
    url = f"http://127.0.0.1:{args.port}"
    print(f"Grok Bot dashboard running at {url}")
    print("Ctrl+C or closing this window stops the DASHBOARD only. A started bot keeps running in the background;")
    print("stop it with the Stop button (reopen the dashboard any time - it reconnects automatically).")
    if not PASSWORD:
        print("Tip: set DASHBOARD_PASSWORD=... in .env to require a password.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    def _catch_up_learning():
        try:
            if learn_daily.due(ROOT):
                print("Last daily learning run is more than 24 hours old. Starting a catch-up run.")
                learn_daily.run(ROOT, on_progress=print)
        except Exception as exc:
            print(f"Daily learning catch-up did not finish: {exc}")

    threading.Thread(target=_catch_up_learning, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
