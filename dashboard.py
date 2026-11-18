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
    server_version = "GrokBotDashboard/2.0"

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
<form method=post action=/login><h3>Grok Bot · SOL Futures</h3><!--ERR--><input type=password name=password placeholder="Dashboard password" autofocus>
<button>Log in</button></form></body></html>"""

def page_html() -> str:
    f = ROOT / "dashboard.html"
    return f.read_text(encoding="utf-8") if f.exists() else "<h1>dashboard.html missing</h1>"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "8080")))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    STATE.mkdir(exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Grok Bot dashboard running at {url}")
    print("Ctrl+C or closing this window stops the DASHBOARD only. A started bot keeps running in the background;")
    print("stop it with the Stop button (reopen the dashboard any time - it reconnects automatically).")
    if not PASSWORD:
        print("Tip: set DASHBOARD_PASSWORD=... in .env to require a password.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
