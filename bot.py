#!/usr/bin/env python3
"""
Bybit SOLUSDT (USDT perpetual) Smart Money Concepts trading bot.

  python bot.py              # run forever (DRY_RUN by default - see config.yaml)
  python bot.py --once       # evaluate the latest closed candle once and exit
  python bot.py --iterations 3
  python bot.py --data-source okx   # public OKX candles (DRY_RUN only, for geo-blocked testing)

DRY_RUN=true  -> fetches market data, evaluates signals, logs would-be orders (paper-tracked).
DRY_RUN=false -> LIVE: sets leverage/isolated margin and places limit orders with SL/TP attached.

The bot writes state/status.json (heartbeat + live status) and state/trades.json
(trade journal) and accepts commands as JSON files dropped in state/commands/
(used by dashboard.py): pause, resume, cancel_pending, close_position, evaluate_now, stop.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pybit.exceptions import FailedRequestError, InvalidRequestError
from pybit.unified_trading import HTTP

import market_data
import risk
import smc
from strategy import run_strategy

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
CMD_DIR = STATE_DIR / "commands"
CANDLES_FILE = STATE_DIR / "candles.json"
STATUS_FILE = STATE_DIR / "status.json"
TRADES_FILE = STATE_DIR / "trades.json"
log = logging.getLogger("smcbot")
_running = True


# --------------------------------------------------------------------------- #
def _env_bool(name: str):
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return None
    return v.strip().lower() in ("1", "true", "yes", "on")


def load_config(path: Path) -> dict:
    load_dotenv(ROOT / ".env")
    with open(path) as f:
        cfg = yaml.safe_load(f)
    for key, env in (("dry_run", "BOT_DRY_RUN"), ("testnet", "BOT_TESTNET")):
        v = _env_bool(env)
        if v is not None:
            cfg[key] = v
    return cfg


def setup_logging(cfg: dict):
    lf = ROOT / cfg["logging"]["file"]
    lf.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s UTC %(levelname)-7s %(message)s")
    fmt.converter = time.gmtime
    log.setLevel(cfg["logging"].get("level", "INFO"))
    fh = RotatingFileHandler(lf, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    # SMCBOT_QUIET=1 (set by dashboard.py) -> file log only, no duplicate console output
    log.handlers[:] = [fh] if os.getenv("SMCBOT_QUIET") == "1" else [fh, sh]


def write_json_atomic(path: Path, data) -> None:
    """Write JSON via temp file + replace; retried because Windows may briefly lock the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    try:
        tmp.unlink()
    except OSError:
        pass


def read_json(path: Path, default):
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, PermissionError):
            time.sleep(0.05)
    return default


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
class Bot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.symbol = cfg["symbol"]
        self.cat = cfg["category"]
        self.dry = bool(cfg["dry_run"])
        self.testnet = bool(cfg["testnet"])
        self.mode = "DRY_RUN" if self.dry else ("TESTNET" if self.testnet else "LIVE")
        self.rc = cfg["risk"]
        key, secret = os.getenv("BYBIT_API_KEY"), os.getenv("BYBIT_API_SECRET")
        self.has_keys = bool(key and secret)
        if not self.dry and not self.has_keys:
            raise SystemExit("LIVE mode needs BYBIT_API_KEY and BYBIT_API_SECRET in the environment/.env")
        kw = dict(testnet=self.testnet, recv_window=10000)
        if self.has_keys:
            kw.update(api_key=key, api_secret=secret)
        self.http = HTTP(**kw)
        self.state_path = ROOT / cfg["state_file"]
        self.state = self._load_state()
        self.trades = read_json(TRADES_FILE, [])
        self.rules = risk.InstrumentRules()
        self.last_eval_ts = None
        self.status = {
            "pid": os.getpid(), "started_at": now_iso(), "mode": self.mode, "dry_run": self.dry,
            "testnet": self.testnet, "symbol": self.symbol,
            "data_source": cfg.get("market_data_source", "bybit"),
            "htf_interval": cfg["htf_interval"], "ltf_interval": cfg["ltf_interval"],
            "risk_usdt": self.rc["risk_usdt"], "leverage": self.rc["leverage"],
            "max_daily_loss_usdt": self.rc.get("max_daily_loss_usdt"),
            "max_trades_per_day": self.rc.get("max_trades_per_day"),
            "price": None, "htf_bias": None, "last_bar": None, "last_eval_at": None,
            "pending_order": None, "position": None, "balance_usdt": None,
            "today_pnl": None, "trades_today": 0, "paused": self.state.get("paused", False),
            "last_error": None, "last_command": None, "running": True,
        }

    # ------------------------------------------------------------- state ----
    def _load_state(self) -> dict:
        st = read_json(self.state_path, {})
        st.setdefault("handled_setups", [])
        st.setdefault("trades_today", {})
        st.setdefault("paused", False)
        return st

    def _save_state(self):
        self.state["handled_setups"] = self.state["handled_setups"][-500:]
        write_json_atomic(self.state_path, self.state)

    def _save_trades(self):
        self.trades = self.trades[-1000:]
        write_json_atomic(TRADES_FILE, self.trades)

    def write_status(self):
        self.status["heartbeat"] = now_iso()
        self.status["heartbeat_epoch"] = time.time()
        self.status["paused"] = self.state.get("paused", False)
        self.status["trades_today"] = self.state["trades_today"].get(self._today(), 0)
        write_json_atomic(STATUS_FILE, self.status)

    # ------------------------------------------------------------- setup ----
    def load_instrument(self):
        try:
            r = self.http.get_instruments_info(category=self.cat, symbol=self.symbol)
            it = r["result"]["list"][0]
            lf, pf, lev = it["lotSizeFilter"], it["priceFilter"], it["leverageFilter"]
            self.rules = risk.InstrumentRules(
                qty_step=float(lf["qtyStep"]), min_qty=float(lf["minOrderQty"]),
                max_qty=float(lf.get("maxMktOrderQty") or lf["maxOrderQty"]),
                tick_size=float(pf["tickSize"]), min_notional=float(lf.get("minNotionalValue") or 5),
                max_leverage=float(lev["maxLeverage"]))
            log.info("Instrument %s: %s", self.symbol, self.rules)
        except Exception as e:
            if self.dry:
                log.warning("Could not load instrument info from Bybit (%s). Using defaults %s",
                            str(e).splitlines()[0][:200], self.rules)
            else:
                raise

    def prepare_account(self):
        if self.dry:
            log.info("DRY_RUN: skipping leverage / margin-mode changes")
            return
        lev = str(min(float(self.rc["leverage"]), self.rules.max_leverage))
        if self.rc.get("margin_mode", "isolated") == "isolated":
            try:
                self.http.switch_margin_mode(category=self.cat, symbol=self.symbol, tradeMode=1,
                                             buyLeverage=lev, sellLeverage=lev)
                log.info("Margin mode set to ISOLATED")
            except Exception as e:
                log.warning("Could not switch %s to isolated via API (%s). On a Unified account set "
                            "Isolated Margin in the Bybit app/web for this symbol/account.", self.symbol, e)
        try:
            self.http.set_leverage(category=self.cat, symbol=self.symbol, buyLeverage=lev, sellLeverage=lev)
            log.info("Leverage set to %sx", lev)
        except Exception as e:
            if "110043" in str(e):
                log.info("Leverage already %sx", lev)
            else:
                log.warning("set_leverage failed: %s", e)

    # -------------------------------------------------------------- data ----
    def fetch(self):
        c = self.cfg
        if c.get("market_data_source", "bybit") == "okx":
            ltf = market_data.okx_klines(self.symbol, c["ltf_interval"], c["ltf_bars"])
            htf = market_data.okx_klines(self.symbol, c["htf_interval"], c["htf_bars"])
        else:
            ltf = market_data.bybit_klines(self.http, self.symbol, c["ltf_interval"], c["ltf_bars"], self.cat)
            htf = market_data.bybit_klines(self.http, self.symbol, c["htf_interval"], c["htf_bars"], self.cat)
        return ltf, htf

    def last_price(self, fallback=None):
        try:
            if self.cfg.get("market_data_source", "bybit") == "okx":
                return market_data.okx_last_price(self.symbol)
            r = self.http.get_tickers(category=self.cat, symbol=self.symbol)
            return float(r["result"]["list"][0]["lastPrice"])
        except Exception:
            return fallback

    # ----------------------------------------------------------- account ----
    def open_position(self):
        r = self.http.get_positions(category=self.cat, symbol=self.symbol)
        for p in r["result"]["list"]:
            if float(p.get("size") or 0) > 0:
                return p
        return None

    def bot_orders(self):
        r = self.http.get_open_orders(category=self.cat, symbol=self.symbol, openOnly=0)
        return [o for o in r["result"]["list"] if (o.get("orderLinkId") or "").startswith("smc")]

    def available_usdt(self):
        for acct in ("UNIFIED", "CONTRACT"):
            try:
                r = self.http.get_wallet_balance(accountType=acct, coin="USDT")
                a = r["result"]["list"][0]
                if acct == "UNIFIED" and a.get("totalAvailableBalance") not in (None, ""):
                    return float(a["totalAvailableBalance"])
                coin = a["coin"][0]
                for k in ("availableToWithdraw", "walletBalance"):
                    if coin.get(k) not in (None, ""):
                        return float(coin[k])
            except Exception:
                continue
        return None

    def closed_pnl_since(self, start_ms: int):
        r = self.http.get_closed_pnl(category=self.cat, symbol=self.symbol, startTime=int(start_ms), limit=100)
        return r["result"]["list"]

    def _today_start_ms(self) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp() * 1000)

    def todays_pnl(self) -> float:
        if self.dry:
            today = self._today()
            return round(sum(t.get("pnl_usdt") or 0 for t in self.trades
                             if t.get("mode") == self.mode and (t.get("closed_at") or "").startswith(today)), 4)
        return sum(float(x["closedPnl"]) for x in self.closed_pnl_since(self._today_start_ms()))

    # ------------------------------------------------------------ guards ----
    def _today(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def guards_ok(self) -> bool:
        if self.state.get("paused"):
            log.info("Entries PAUSED (resume from the dashboard) - not opening new trades")
            return False
        mt = int(self.rc.get("max_trades_per_day") or 0)
        if mt and self.state["trades_today"].get(self._today(), 0) >= mt:
            log.info("Max trades per day (%d) reached - no new entries today (UTC)", mt)
            return False
        ml = float(self.rc.get("max_daily_loss_usdt") or 0)
        if ml:
            pnl = self.todays_pnl()
            if pnl <= -ml:
                log.warning("Daily loss limit hit (%.2f USDT <= -%.2f) - no new entries today (UTC)", pnl, ml)
                return False
        return True

    # ----------------------------------------------------------- journal ----
    def _trade(self, sid):
        for t in reversed(self.trades):
            if t["id"] == sid:
                return t
        return None

    def _journal_new(self, st, sz, sid, order_id=None):
        self.trades.append(dict(
            id=sid, mode=self.mode, symbol=self.symbol, side=st.side, created_at=fmt_ms(st.created_ts),
            entry=st.entry, stop=st.stop, tp=st.tp, qty=sz.qty, risk_usdt=round(sz.risk_usdt, 4),
            est_fees=round(sz.fees_usdt, 4), status="pending", order_id=order_id, opened_at=None,
            closed_at=None, exit_price=None, pnl_usdt=None, r=None, reason=st.reason))
        d = self._today()
        self.state["trades_today"][d] = self.state["trades_today"].get(d, 0) + 1
        self._save_trades()

    def _close_trade(self, t, exit_price, status=None, pnl=None):
        d = 1 if t["side"] == "Buy" else -1
        if pnl is None:   # paper: price PnL minus estimated fees
            fees = t["qty"] * (t["entry"] * float(self.rc["maker_fee"]) + exit_price * float(self.rc["taker_fee"]))
            pnl = t["qty"] * (exit_price - t["entry"]) * d - fees
        t.update(exit_price=round(float(exit_price), 6), pnl_usdt=round(float(pnl), 4),
                 r=round(float(pnl) / float(self.rc["risk_usdt"]), 3), closed_at=now_iso(),
                 status=status or ("win" if pnl > 0 else "loss"))

    def _sync_dry_journal(self, setups):
        by_id = {self._setup_id(s): s for s in setups}
        for t in self.trades:
            if t.get("mode") != self.mode or t["status"] not in ("pending", "open"):
                continue
            s = by_id.get(t["id"])
            if s is None:
                continue
            if s.status == "filled" and t["status"] == "pending":
                t.update(status="open", opened_at=now_iso())
            elif s.status in ("win", "loss"):
                if t["opened_at"] is None:
                    t["opened_at"] = now_iso()
                self._close_trade(t, s.exit_price, s.status)
            elif s.status in ("expired", "invalidated"):
                t.update(status="expired", closed_at=now_iso())
        self._save_trades()

    def _sync_live_journal(self, pos, orders):
        changed = False
        ids = {o["orderLinkId"] for o in orders}
        for t in self.trades:
            if t.get("mode") != self.mode or t["status"] not in ("pending", "open"):
                continue
            if t["status"] == "pending":
                if t["id"] in ids:
                    continue
                if pos:
                    t.update(status="open", opened_at=now_iso())
                    changed = True
                    continue
            # no working order and no position -> the trade ended (or the order was cancelled)
            if pos is None:
                since = int(datetime.strptime(t["created_at"], "%Y-%m-%dT%H:%M:%SZ")
                            .replace(tzinfo=timezone.utc).timestamp() * 1000)
                recs = self.closed_pnl_since(since)
                if recs:
                    rec = recs[0]   # newest first
                    if t["opened_at"] is None:
                        t["opened_at"] = fmt_ms(int(rec.get("createdTime") or since))
                    self._close_trade(t, float(rec.get("avgExitPrice") or 0), pnl=float(rec["closedPnl"]))
                else:
                    t.update(status="cancelled", closed_at=now_iso())
                changed = True
        if changed:
            self._save_trades()

    def _refresh_views(self, price):
        """Fill status.pending_order / position from the journal (paper) or exchange (live)."""
        if self.dry:
            pend = next((t for t in reversed(self.trades) if t.get("mode") == self.mode and t["status"] == "pending"), None)
            opn = next((t for t in reversed(self.trades) if t.get("mode") == self.mode and t["status"] == "open"), None)
            self.status["pending_order"] = {k: pend[k] for k in ("id", "side", "entry", "stop", "tp", "qty", "created_at")} if pend else None
            if opn:
                d = 1 if opn["side"] == "Buy" else -1
                upnl = (price - opn["entry"]) * d * opn["qty"] if price else None
                self.status["position"] = dict(side=opn["side"], size=opn["qty"], entry=opn["entry"], stop=opn["stop"],
                                               tp=opn["tp"], unrealized_pnl=round(upnl, 4) if upnl is not None else None,
                                               paper=True)
            else:
                self.status["position"] = None
            if self.has_keys:
                self.status["balance_usdt"] = self.available_usdt()
        else:
            pos, orders = self.open_position(), self.bot_orders()
            o = next((x for x in orders if x.get("orderStatus") in ("New", "PartiallyFilled")), None)
            self.status["pending_order"] = dict(id=o["orderLinkId"], side=o["side"], entry=float(o["price"]),
                                                stop=o.get("stopLoss"), tp=o.get("takeProfit"), qty=o["qty"]) if o else None
            self.status["position"] = dict(side=pos["side"], size=pos["size"], entry=pos.get("avgPrice"),
                                           stop=pos.get("stopLoss"), tp=pos.get("takeProfit"),
                                           unrealized_pnl=pos.get("unrealisedPnl"), paper=False) if pos else None
            self.status["balance_usdt"] = self.available_usdt()
        try:
            self.status["today_pnl"] = round(self.todays_pnl(), 4)
        except Exception as e:
            log.debug("today pnl failed: %s", e)

    # ---------------------------------------------------------- evaluate ----
    def evaluate(self):
        ltf, htf = self.fetch()
        if len(ltf) < 200 or len(htf) < 50:
            log.warning("Not enough candles (ltf=%d htf=%d)", len(ltf), len(htf))
            return
        last_ts = int(ltf["ts"].iloc[-1])
        setups = run_strategy(ltf, htf, self.cfg, self.rules.tick_size)
        s = self.cfg["strategy"]
        htf_trend = smc.analyze(htf, s["htf_swing_left"], s["htf_swing_right"], s["sweep_lookback"]).trend
        bias = int(htf_trend[-1])
        close = float(ltf["close"].iloc[-1])
        bar_time = datetime.fromtimestamp(last_ts / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        pending = setups[-1] if setups and setups[-1].status == "pending" else None
        log.info("Evaluated closed %sm bar %s close=%.3f | HTF bias %s | setups in window=%d | pending=%s",
                 self.cfg["ltf_interval"], bar_time, close, {1: "BULLISH", -1: "BEARISH"}.get(bias, "NEUTRAL"),
                 len(setups), f"{pending.side} @ {pending.entry} SL {pending.stop} TP {pending.tp}" if pending else "none")
        self.last_eval_ts = last_ts
        try:   # recent candles for the dashboard chart
            tail_df = ltf.tail(300)
            write_json_atomic(CANDLES_FILE, {
                "written": time.time(), "symbol": self.symbol, "interval": int(self.cfg["ltf_interval"]),
                "source": self.cfg.get("market_data_source", "bybit"),
                "candles": [[int(r.ts), r.open, r.high, r.low, r.close, r.volume] for r in tail_df.itertuples()]})
        except Exception as e:
            log.debug("could not write candles: %s", e)
        self.status.update(htf_bias={1: "BULLISH", -1: "BEARISH"}.get(bias, "NEUTRAL"), last_bar=bar_time,
                           last_eval_at=now_iso(), price=self.last_price(close) or close)

        if self.dry:
            self._sync_dry_journal(setups)
            self._dry_handle(pending, close)
        else:
            self._live_handle(pending, close)
        self._save_state()
        self._refresh_views(self.status["price"])

    def _setup_id(self, st) -> str:
        return f"smc{st.created_ts}{'L' if st.direction > 0 else 'S'}"

    def _size(self, st, avail=None):
        return risk.position_size(st.entry, st.stop, float(self.rc["risk_usdt"]), self.rules,
                                  float(self.rc["leverage"]), bool(self.rc.get("include_fees_in_risk", True)),
                                  float(self.rc["maker_fee"]), float(self.rc["taker_fee"]), avail)

    def _dry_handle(self, st, close):
        if st is None:
            return
        sid = self._setup_id(st)
        if sid in self.state["handled_setups"]:
            log.info("DRY_RUN: setup %s already logged", sid)
            return
        if any(t.get("mode") == self.mode and t["status"] in ("pending", "open") for t in self.trades):
            log.info("DRY_RUN: paper order/position already active - skip %s", sid)
            return
        if not self.guards_ok():
            return
        sz = self._size(st)
        if not sz.ok:
            log.info("DRY_RUN: SKIP setup %s - %s", sid, sz.reason)
        else:
            log.info("DRY_RUN WOULD PLACE: %s %s Limit qty=%s price=%s SL=%s TP=%s | risk=%.2f USDT "
                     "(+~%.2f fees) notional=%.2f margin@%sx=%.2f | %s",
                     self.symbol, st.side, sz.qty, st.entry, st.stop, st.tp, sz.risk_usdt, sz.fees_usdt,
                     sz.notional, self.rc["leverage"], sz.margin, st.reason)
            self._journal_new(st, sz, sid)
        self.state["handled_setups"].append(sid)

    def _live_handle(self, st, close):
        pos = self.open_position()
        orders = self.bot_orders()
        self._sync_live_journal(pos, orders)
        if pos:
            log.info("Open position: %s %s @ %s (SL %s TP %s, uPnL %s) - managed by exchange SL/TP",
                     pos["side"], pos["size"], pos.get("avgPrice"), pos.get("stopLoss"),
                     pos.get("takeProfit"), pos.get("unrealisedPnl"))
            for o in orders:   # leftover entry orders while in a position -> cancel
                if o.get("reduceOnly") is False and o["orderStatus"] in ("New", "PartiallyFilled") \
                        and float(o.get("cumExecQty") or 0) == 0:
                    self._cancel(o, "position already open")
            return
        want = self._setup_id(st) if st else None
        for o in orders:
            if o["orderLinkId"] != want:
                self._cancel(o, "setup expired/invalidated/replaced")
                t = self._trade(o["orderLinkId"])
                if t and t["status"] == "pending":
                    t.update(status="expired", closed_at=now_iso())
                    self._save_trades()
            else:
                log.info("Entry order %s still working (%s %s @ %s)", want, o["side"], o["qty"], o["price"])
                return
        if st is None or want in self.state["handled_setups"]:
            return
        if not self.guards_ok():
            return
        price = self.last_price(close)
        if (st.direction > 0 and (price <= st.stop or price >= st.tp)) or \
           (st.direction < 0 and (price >= st.stop or price <= st.tp)):
            log.info("Skip %s: price %.3f already beyond SL/TP", want, price)
            self.state["handled_setups"].append(want)
            return
        sz = self._size(st, self.available_usdt())
        if not sz.ok:
            log.info("SKIP setup %s - %s", want, sz.reason)
            self.state["handled_setups"].append(want)
            return
        fmt = lambda x: f"{x:.10f}".rstrip("0").rstrip(".")
        r = self.http.place_order(
            category=self.cat, symbol=self.symbol, side=st.side, orderType="Limit",
            qty=fmt(sz.qty), price=fmt(st.entry), timeInForce="GTC", positionIdx=0,
            orderLinkId=want, takeProfit=fmt(st.tp), stopLoss=fmt(st.stop), tpslMode="Full",
            tpTriggerBy="LastPrice", slTriggerBy="LastPrice")
        log.info("PLACED %s %s qty=%s @ %s SL=%s TP=%s risk=%.2f USDT (+~%.2f fees) orderId=%s | %s",
                 st.side, self.symbol, sz.qty, st.entry, st.stop, st.tp, sz.risk_usdt, sz.fees_usdt,
                 r["result"].get("orderId"), st.reason)
        self.state["handled_setups"].append(want)
        self._journal_new(st, sz, want, r["result"].get("orderId"))

    def _cancel(self, o, why):
        try:
            self.http.cancel_order(category=self.cat, symbol=self.symbol, orderId=o["orderId"])
            log.info("Cancelled order %s (%s)", o["orderLinkId"], why)
        except Exception as e:
            log.warning("Cancel %s failed: %s", o["orderLinkId"], e)

    # ---------------------------------------------------------- commands ----
    def process_commands(self) -> bool:
        """Handle command files from the dashboard. Returns True if an evaluation was requested."""
        global _running
        want_eval = False
        if not CMD_DIR.exists():
            return False
        for f in sorted(CMD_DIR.glob("*.json")):
            try:
                cmd = json.loads(f.read_text(encoding="utf-8")).get("command", "")
            except Exception:
                cmd = ""
            try:
                f.unlink()
            except OSError:
                continue
            try:
                msg = self.run_command(cmd)
                if cmd == "evaluate_now":
                    want_eval = True
                if cmd == "stop":
                    _running = False
            except Exception as e:
                msg = f"command {cmd} failed: {e}"
                log.exception(msg)
            log.info("Dashboard command '%s': %s", cmd, msg)
            self.status["last_command"] = {"command": cmd, "result": msg, "at": now_iso()}
        return want_eval

    def run_command(self, cmd: str) -> str:
        if cmd == "pause":
            self.state["paused"] = True
            self._save_state()
            return "new entries paused (existing order/position untouched)"
        if cmd == "resume":
            self.state["paused"] = False
            self._save_state()
            return "new entries resumed"
        if cmd in ("evaluate_now", "stop"):
            return "ok"
        if cmd == "cancel_pending":
            if self.dry:
                n = 0
                for t in self.trades:
                    if t.get("mode") == self.mode and t["status"] == "pending":
                        t.update(status="cancelled", closed_at=now_iso())
                        n += 1
                self._save_trades()
                self._refresh_views(self.status.get("price"))
                return f"cancelled {n} paper order(s)"
            orders = [o for o in self.bot_orders() if o.get("orderStatus") in ("New", "PartiallyFilled")]
            for o in orders:
                self._cancel(o, "dashboard request")
                t = self._trade(o["orderLinkId"])
                if t and t["status"] == "pending":
                    t.update(status="cancelled", closed_at=now_iso())
            self._save_trades()
            self._refresh_views(self.status.get("price"))
            return f"cancelled {len(orders)} order(s)"
        if cmd == "close_position":
            if self.dry:
                price = self.last_price(self.status.get("price"))
                n = 0
                for t in self.trades:
                    if t.get("mode") == self.mode and t["status"] == "open" and price:
                        self._close_trade(t, price, "closed")
                        n += 1
                self._save_trades()
                self._refresh_views(price)
                return f"closed {n} paper position(s) at {price}"
            for o in self.bot_orders():
                self._cancel(o, "closing position")
            pos = self.open_position()
            if not pos:
                return "no open position"
            side = "Sell" if pos["side"] == "Buy" else "Buy"
            r = self.http.place_order(category=self.cat, symbol=self.symbol, side=side, orderType="Market",
                                      qty=str(pos["size"]), reduceOnly=True, positionIdx=0)
            self._refresh_views(self.status.get("price"))
            return f"market close sent ({side} {pos['size']}), orderId={r['result'].get('orderId')}"
        return f"unknown command '{cmd}'"

    # -------------------------------------------------------------- loop ----
    def run(self, iterations: int = 0, once: bool = False):
        global _running
        mode = "DRY_RUN (no orders)" if self.dry else "*** LIVE TRADING ***"
        log.info("Starting SMC bot %s %s | %s | %s | HTF %sm / LTF %sm | risk %.2f USDT/trade | lev %sx | data=%s",
                 self.symbol, self.cat, mode, "TESTNET" if self.testnet else "MAINNET",
                 self.cfg["htf_interval"], self.cfg["ltf_interval"], float(self.rc["risk_usdt"]),
                 self.rc["leverage"], self.cfg.get("market_data_source", "bybit"))
        if not once:
            CMD_DIR.mkdir(parents=True, exist_ok=True)
            for f in CMD_DIR.glob("*.json"):     # drop stale commands from a previous run
                f.unlink(missing_ok=True)
            self.write_status()
        if not self.dry:
            log.warning("LIVE mode: real orders will be sent in 10 seconds. Ctrl+C to abort.")
            for _ in range(10):
                if not _running:
                    break
                time.sleep(1)
        self.load_instrument()
        self.prepare_account()
        ltf_ms = int(self.cfg["ltf_interval"]) * 60_000
        tick = 2.0                              # loop granularity (commands / heartbeat)
        poll = float(self.cfg["poll_seconds"])
        n, backoff_until, backoff, last_check, last_price = 0, 0.0, 5, 0.0, 0.0
        while _running:
            want_eval = False if once else self.process_commands()
            if not _running:
                break
            now = time.time()
            latest_closed_open = (int(now * 1000) // ltf_ms) * ltf_ms - ltf_ms
            due = (self.last_eval_ts is None or latest_closed_open > self.last_eval_ts) and now - last_check >= poll
            if (once or iterations or due or want_eval) and now >= backoff_until:
                last_check = now
                try:
                    self.evaluate()
                    backoff = 5
                    self.status["last_error"] = None
                except (FailedRequestError, InvalidRequestError) as e:
                    msg = str(e).splitlines()[0]
                    log.error("Bybit API error (retry in %ss): %s", backoff, msg)
                    self.status["last_error"] = msg[:300]
                    backoff_until = time.time() + backoff
                    backoff = min(backoff * 2, 300)
                except Exception as e:
                    log.exception("Evaluation error: %s", e)
                    self.status["last_error"] = str(e)[:300]
                    backoff_until = time.time() + backoff
                    backoff = min(backoff * 2, 300)
                n += 1
                if once or (iterations and n >= iterations):
                    break
            elif now - last_price >= 15 and self.last_eval_ts is not None:
                last_price = now
                p = self.last_price()
                if p:
                    self.status["price"] = p
                    pos = self.status.get("position")
                    if pos and pos.get("paper"):
                        d = 1 if pos["side"] == "Buy" else -1
                        pos["unrealized_pnl"] = round((p - pos["entry"]) * d * pos["size"], 4)
            if not once:
                self.write_status()
            time.sleep(tick)
        self.status["running"] = False
        if not once:
            self.write_status()
        log.info("Bot stopped")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--once", action="store_true", help="evaluate once and exit")
    ap.add_argument("--iterations", type=int, default=0, help="evaluate N times then exit (smoke test)")
    ap.add_argument("--data-source", choices=["bybit", "okx"], default=None,
                    help="override market_data_source (okx = public fallback feed; DRY_RUN only)")
    args = ap.parse_args()
    cfg = load_config(Path(args.config))
    if args.data_source:
        cfg["market_data_source"] = args.data_source
    if cfg.get("market_data_source") == "okx" and not cfg["dry_run"]:
        raise SystemExit("market_data_source=okx is only allowed with dry_run=true; live trading uses Bybit data.")
    setup_logging(cfg)

    def _stop(*_):
        global _running
        _running = False
        log.info("Shutdown signal received")
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGBREAK"):          # Windows Ctrl+Break
        signal.signal(signal.SIGBREAK, _stop)
    Bot(cfg).run(iterations=args.iterations, once=args.once)


if __name__ == "__main__":
    main()
