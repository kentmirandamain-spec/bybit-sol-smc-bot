# Bybit SOLUSDT Smart Money Concepts Bot

Trades **SOLUSDT USDT-perpetual (linear) futures on Bybit** with a Smart Money Concepts
model, risking a **fixed 5 USDT per trade** whatever your balance.

> ⚠️ Futures trading can lose money fast. The backtest included here was **negative** on the last
> 90 days with default settings (see "Backtest" below). Run in `dry_run` (and/or testnet) first.
> No strategy is guaranteed to make a profit.

## How it trades
| Step | Timeframe | Logic |
|---|---|---|
| Bias | HTF (default 1h) | Market structure from fractal swings: close above last swing high = bullish BOS/CHoCH, below last swing low = bearish. Only trades in the HTF trend direction. |
| Liquidity sweep | LTF (default 15m) | A candle wicks beyond a prior swing low (high) and closes back inside = sell-side (buy-side) liquidity swept. |
| Confirmation | LTF | Within `sweep_max_age` bars, a **CHoCH** in the bias direction. |
| Entry | LTF | Limit order at the proximal edge of the **order block** of the leg that broke structure (or the **FVG**, `entry_mode`). |
| Stop | LTF | Beyond the OB / sweep extreme + `stop_buffer_atr` × ATR. |
| Target | LTF | `rr` × risk (default **2R**) or opposing liquidity (`tp_mode: liquidity`). |
| Expiry | LTF | Unfilled order cancelled after `entry_expiry_bars`, on an opposite CHoCH, or if price reaches TP first. |

Risk rules: size = `5 / |entry − stop|` (fees included by default so the stop costs ≈5 USDT),
floored to Bybit's qty step, skipped if below min qty / min notional or not enough margin.
SL and TP are attached to every order (exchange-side). One position/order at a time.
Optional daily loss limit (`max_daily_loss_usdt`) and max trades per day. Leverage (default 5x)
and isolated margin are set via the API on start.

## Files
| File | Purpose |
|---|---|
| `bot.py` | Live/dry-run bot loop (pybit v5) |
| `smc.py` | SMC indicators: swings, BOS/CHoCH, order blocks, FVGs, liquidity sweeps |
| `strategy.py` | Entry model (shared by bot and backtest) |
| `risk.py` | Fixed-USDT position sizing + tick/step rounding |
| `market_data.py` | Kline downloads (Bybit; OKX public fallback for testing) |
| `backtest.py` | Backtest on public klines (no keys) |
| `config.yaml` | All settings incl. `dry_run`, `testnet` |
| `.env.example` | API key template |
| `smcbot.service` | systemd unit for 24/7 |
| `dashboard.py` / `dashboard.html` | Local web dashboard (http://127.0.0.1:8080) |
| `updater.py` | Download updates from GitHub main (no git required) |
| `VERSION` / `CHANGELOG.md` | Installed version and what changed |
| `start_dashboard.bat` | Windows double-click launcher |

## 🖥️ Easy mode: the Grok Bot dashboard (Windows, beginners start here)
A local trading-terminal style web page (Overview · Trades · Settings · Backtest · Logs). The Overview
chart is a TradingView chart of **BYBIT:SOLUSDT.P** (dark theme, Asia/Manila, interval = the bot's
entry timeframe). Entry, stop and target are listed next to it, because that free widget cannot draw
them. **Bot chart (with trade levels)** is the built-in canvas, with those lines and past fills, and
it is what you see if TradingView does not load. The Grok mark is inlined. Nothing extra to install:
the page uses Python's built-in web server plus the bot's normal requirements. TradingView is the one
piece that needs internet (`https://s3.tradingview.com/tv.js`).

1. Install **Python 3.11+** from python.org (tick **"Add python.exe to PATH"**).
2. Unzip the folder somewhere like `C:\Users\<you>\bybit-sol-smc-bot`.
3. **Double-click `start_dashboard.bat`.** The first run creates `.venv`, installs the requirements
   and creates `.env`. Your browser opens **http://127.0.0.1:8080**. Keep the black window open while you use it.
4. Put your Bybit API key in `.env` (open it with Notepad). The dashboard never shows or edits keys.
   Optional: add `DASHBOARD_PASSWORD=...` to `.env` to require a password.

What you can do on the page:
- **Monitor:** running/stopped, mode badge (blue **DRY RUN** / amber **TESTNET** / red pulsing **LIVE**),
  heartbeat, SOL price, 1h bias, pending order, open position with unrealized PnL, USDT balance, today's
  PnL and trades vs your limits, trade history (dry-run "would-be" trades too) with win rate, total R and PnL,
  and the live log. It refreshes every 3 seconds.
- **Commands:** Start / Stop bot, Pause / Resume new entries, Cancel pending order, Close position at market
  (asks you to confirm), Check signals now, Run backtest (choose the number of days).
- **Settings:** risk per trade, leverage, R multiple, daily loss limit, max trades/day, timeframes, CHoCH-only,
  HTF bias, entry zone (OB/FVG), TP mode, dry run, testnet. They're saved to `config.yaml` (a backup goes to
  `config.yaml.bak`) and take effect the next time you start the bot. Turning on **LIVE** asks you to type `GO LIVE`.

Notes:
- Closing the dashboard window does **not** stop a running bot. Reopen the dashboard and press **Stop**.
- The PC must stay on and awake for the bot to trade (set Windows sleep to "Never" while it runs).
  Bybit blocks some countries/IPs, and the bot needs your PC's IP on the API key whitelist.
- "Data: OKX" in the Controls box is only for testing in dry run where Bybit is blocked. Live trading always uses Bybit.
- If Windows **Smart App Control** blocks Python packages: the dashboard adds no new packages, but the bot itself
  needs `pandas`/`numpy`, which ship compiled files. If SAC blocks them, either turn SAC off
  (Windows Security → App & browser control → Smart App Control) or run the bot on a VPS (see section 4).
- Files the dashboard reads: `state/status.json` (heartbeat), `state/trades.json` (trade journal),
  `logs/bot.log`. Commands are passed as small files in `state/commands/`.

## Updating the bot

The dashboard updates itself from the `main` branch of
[kentmirandamain-spec/bybit-sol-smc-bot](https://github.com/kentmirandamain-spec/bybit-sol-smc-bot).
You do not need git installed, and the updater uses only Python's standard library.

1. Open the dashboard and go to **Settings → About & updates**. The page shows the version from the `VERSION` file.
2. **Check for updates** compares that version (and, after the first successful check or update, the recorded commit) with `main`.
   You'll see what's new: commit messages from GitHub, and the part of `CHANGELOG.md` above your current version.
3. **Update** asks you to confirm, then downloads the main-branch zip and replaces program files in this folder.

What is kept:

- `.env` (API keys and `GITHUB_TOKEN`)
- your `config.yaml` values — new keys from the updated default config are added, existing values and comments stay
- `logs/`, `state/` (trade journal, bot status) and `.venv/`

Every replaced file is copied to `backups/<timestamp>/` first. **Roll back** restores the newest backup.
The install stamp `state/update.json` (version + commit only, no secrets) is the one file under `state/` the updater writes.

The update is refused while the bot is **running with an open position or a pending order**. Press **Stop** first.
Stopping the bot does not cancel a stop-loss or take-profit already sitting on the exchange.

If `requirements.txt` changed, the dashboard runs `pip install -r requirements.txt` with the **same Python** and shows the output.
That is the only time an update installs packages. On Windows, after a successful update the dashboard tries to restart itself.
Otherwise close the window and double-click `start_dashboard.bat` again. If the bot was running (flat, with no position), stop and start it too so it loads the new code.

The repository is public, so no token is required. If you later make it private, put a read-only token in `.env`:

```
GITHUB_TOKEN=github_pat_...
```

A fine-grained personal access token with access to this repository and **Contents: Read-only** is enough. The token is sent only to GitHub and is not written into the log or the page.

If an update won't start afterwards, copy the files from the newest `backups/<timestamp>/` folder back over this directory. Do not copy that backup over `.env`, `logs/`, `state/` or `.venv/`.

## 1. Create the Bybit API key (do this carefully)
1. Bybit → profile → **API** → *Create New Key* → **System-generated API Keys**.
2. Permissions: **Read-Write**, tick **Contract → Orders + Positions** (Unified Trading). **Nothing else.**
   **Do NOT enable Withdrawal, Transfer or Sub-account.**
3. **IP restriction: bind it to your server's IP** (strongly recommended; keys without IP binding expire after 90 days).
4. Use a **one-way position mode** for SOLUSDT (default). Keep only bot funds in the account/sub-account.
5. For testnet, create a separate key at https://testnet.bybit.com.

## 2. Install
```bash
unzip bybit-sol-smc-bot.zip && cd bybit-sol-smc-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env        # paste BYBIT_API_KEY / BYBIT_API_SECRET  (never share this file)
chmod 600 .env
```

## 3. Run
```bash
python backtest.py --days 90            # sanity check (public data, no keys)
python bot.py --once                    # one evaluation, dry run
python bot.py                           # run forever (dry run by default)
```
Logs go to `logs/bot.log` (rotating) and the console. Times in logs are UTC.

### Going LIVE
1. Watch the dry run for a while; every would-be order is logged as `DRY_RUN WOULD PLACE ...`.
2. Optional: test real order flow on testnet: `testnet: true`, `dry_run: false`, testnet keys in `.env`.
3. Live: in `config.yaml` set `dry_run: false` and `testnet: false` (or `BOT_DRY_RUN=false` in `.env`).
4. Start `python bot.py`. It prints `*** LIVE TRADING ***` and waits 10 s before doing anything (Ctrl+C to abort).

Margin note: risk is fixed but position size is not. A 0.5% stop at 5 USDT risk is ~1,000 USDT notional
→ ~200 USDT margin at 5x. If you don't have the margin the trade is skipped (logged). Raise `leverage`
only if you understand isolated-margin liquidation: the stop must sit well inside the liquidation price.

## 4. Run 24/7 (VPS)
Use a VPS in a region Bybit serves (Bybit blocks e.g. US IPs), and whitelist its IP on the API key.

**nohup (quick):**
```bash
nohup .venv/bin/python bot.py > logs/nohup.out 2>&1 &
tail -f logs/bot.log          # watch
pkill -f "python bot.py"      # stop
```
**systemd (recommended, restarts on crash/reboot):**
```bash
sudo cp smcbot.service /etc/systemd/system/   # edit User / paths first
sudo systemctl daemon-reload
sudo systemctl enable --now smcbot
journalctl -u smcbot -f
```

## Learning from the journal

The bot writes every setup it sees — taken, skipped, or expired, from a backtest, dry-run, or live run — to `state/journal/` (gitignored). Each row stores the market at the signal: UTC session (Asia 00–06, London 07–11, New York 12–20, otherwise off), hour, weekday, ATR % and ATR percentile, higher-timeframe drift and an ADX-like strength, distance from the higher-timeframe swing, sweep depth, order-block and fair-value-gap size, stop distance, distance to opposing liquidity, volume versus its average, Bybit funding when that public feed answers, and the number of losses in a row before this setup. Closed trades also store net R (fees included, same sizing as `backtest.py`), plus how far price went for and against the trade.

`python learn.py --days 90` (or the **Learning** tab → **Rebuild journal & retrain**) replays that history and runs a walk-forward check:

- The model is an L2-regularized logistic regression implemented in numpy. No scikit-learn and no new compiled packages.
- Its cutoff, if any, is chosen on an inner slice of the older data by **expectancy** (average R per trade, net of fees), not by win rate. Win rate is still reported.
- A filter (skip a session, a side, a weekday, or one of a few fixed feature cuts) is proposed only when that bucket lost money in the training window **and** in the most recent held-out weeks, with a minimum number of trades in both. At most three filters are stacked, and only while the held-out expectancy rises and drawdown does not get worse.
- The score gate stays at 0 until the journal has `learning.min_trades` closed trades (default 100) and the held-out window says the cutoff helps.
- **Apply** in the dashboard is the only way a proposal changes `config.yaml`. A copy of the previous file is kept under `backups/`. Live and testnet runs never apply a proposal on their own. Dry-run applies one automatically only when `learning.auto_apply_dry_run` is true.

### What a 90-day study actually found

Ran 19 Nov 2026. Bybit's public klines were blocked, so the download used OKX SOL-USDT-SWAP. 81 setups, **39** closed trades that fit the 5 USDT size rules. Win rate **25.6%** (95% interval about 15–41%), expectancy **−0.32R**, total **−12.4R**, max drawdown **19.5R**. The last 4 weeks held out only **9** trades and the training window had **30**, under the 40 / 20 minimum, so **no filter and no score cutoff were adopted**. There is no out-of-sample improvement to show. The full window is still a losing strategy; the guard is doing its job by refusing to invent a rule from a handful of trades.

## Backtest notes
`backtest.py` uses Bybit public klines by default. If Bybit blocks your IP use `--source okx`
(OKX SOL-USDT-SWAP – very close to Bybit's price, not identical). Limit fills when price trades
through entry; if SL and TP are both inside one candle the SL is assumed; on the fill candle only the
SL can trigger. Fees: maker entry 0.02%, taker exit 0.055%. Slippage and funding are not modelled.

## Key settings worth testing (in dry run / backtest first)
`require_choch`, `use_htf_bias`, `rr`, `entry_mode`, `tp_mode`, `sweep_max_age`, `stop_buffer_atr`,
`htf_interval`, `ltf_interval`. Don't over-fit to one 90-day window.

### Result at build time (18 Nov 2026, OKX SOL-USDT-SWAP data, defaults, 20 Aug → 18 Nov 2026)
36 trades, 10 wins / 26 losses (27.8%), **−6.00R gross, −9.41R net of fees** (−47 USDT at 5 USDT/trade),
max drawdown 16.55R net. Trade list: `backtest_trades.csv`. In a quick one-window sensitivity check,
`require_choch: false` (+13R gross, 62 trades) and `use_htf_bias: false` (+12R, 78 trades) did better,
but that's a single 90-day sample: treat it as a lead to test, not proof.
