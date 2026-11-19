# Changelog

## Unreleased

- Opt-in `adaptive_mode` (default **false**). When off, entries stay 1h bias and 15m, and nothing in the order path changes. When on, a pre-registered 1h regime either stands aside or selects 1h/15m, 1h/5m, or 4h/1h. Risk stays 5 USDT, the daily loss cap stays 15 USDT, and dry run stays the default. The comparison is in `reports/adaptive_backtest.md`. The mode is not turned on by this change.

## 1.2.0 - 2026-11-19

- Telegram alerts and commands via the Bot API (long polling, standard library only). Only `TELEGRAM_CHAT_ID` is answered. `/close` needs `/close confirm` within 60 seconds. Settings has a test-message button.
- `setup.bat` creates the virtualenv, installs requirements, copies `.env`, and adds a Desktop shortcut. `install_autostart.bat` opens the dashboard at Windows sign-in. The bot starts only when `autostart_bot` is on. Dry run stays dry run.
- Optional phone access on the same Wi-Fi (`dashboard_lan`). It stays off without `DASHBOARD_PASSWORD`. Login attempts are rate-limited.
- Breakeven stop and an optional ATR or 15m trail, plus an optional partial take-profit. Defaults are off. On the last 90 days of OKX SOL (36 closed trades) the baseline was 27.8% wins, −0.29R expectancy, 18.6R drawdown. Breakeven at +1R was −0.23R and 12.1R drawdown on the full window, but the last 30 days were worse (−0.18R vs +0.24R). An ATR trail raised the full-window win rate to 38.9% and expectancy to −0.23R; the last 30 days were −0.15R. A 50% partial at +1R was −0.15R with 9.6R drawdown on the full window and about flat on the last 30 days (+0.01R vs +0.24R).
- High-impact USD news can pause new entries. If the calendar feed is down, the bot logs a warning and keeps trading.
- After 3 consecutive losses, new entries pause for the rest of the UTC day. A losing streak can cut risk, never above 5 USDT. On that SOL sample the circuit skipped one trade and moved expectancy from −0.29R to −0.26R. The last 30 days were unchanged.
- `symbols` defaults to SOLUSDT. BTCUSDT and ETHUSDT can be added in Settings. Each trade still risks at most 5 USDT, and at most 2 positions are open by default. A 90-day OKX check, not enabled by default: BTC 28 trades, 28.6% wins, −0.33R, 14.0R drawdown. ETH 33 trades, 39.4% wins, +0.05R, 10.3R drawdown.
- On startup the bot reloads dry-run state and, when live, flags a Bybit position that is missing a stop or target and cancels stale reduce-only orders. Network errors back off up to five minutes.
- A weekly note is written to `state/reports/weekly_YYYY-WW.md` on Sunday 20:00 Asia/Manila, and summarized on Telegram when that is configured.

## 1.1.0 - 2026-11-19

- Daily learning (`learn_daily.py`) keeps a rolling candle cache and retries a small neighborhood of settings, at most two changes at a time. It replaces `config.yaml` only while `dry_run` is true, and only when the last 30 days have at least 20 trades, higher net R after fees, and a drawdown no more than 20% worse. Live mode writes `state/learning/proposal.json` and waits for Apply. Snapshots go to `state/learning/configs/`, with a log at `state/learning/learning_log.jsonl`.
- Windows: `install_daily_learning.bat` / `uninstall_daily_learning.bat` schedule the pass at 00:15 local time for the current user. The dashboard starts a catch-up if the last run is older than 24 hours. A dry-run bot reloads `config.yaml` on the next bar.
- First run (19 Nov 2026, OKX because Bybit was blocked): current rules on the last 30 days were 9 trades, 44.4% wins, +0.21R expectancy, +1.91R total, 2.97R drawdown. Turning higher-timeframe bias off produced 22 trades, 45.5% wins, +0.25R expectancy, +5.55R total, 2.42R drawdown, so the dry-run on this machine applied that one change. The repository default stays `use_htf_bias: true`; one 30-day window is not a reason to ship a new default. Continuous re-optimization can overfit. Past results do not guarantee future results.

## 1.0.0 - 2026-11-18

- Initial release of the Bybit SOLUSDT Smart Money Concepts bot and the local Grok Bot dashboard.
- Overview can show a TradingView chart of BYBIT:SOLUSDT.P (Asia/Manila, entry-timeframe interval). Entry, stop and target stay listed beside it, and the built-in chart is one toggle away.
- The header uses the Grok mark, inlined so the logo works offline.
- A learning journal records each setup's market features and outcome. Walk-forward proposes filters or a numpy logistic score only when held-out expectancy improves without a worse drawdown. Live trading never applies that on its own. A 90-day OKX study (Bybit blocked) had 39 closed trades, 25.6% wins, −0.32R expectancy, and too few held-out trades to adopt a rule.
- The dashboard can check GitHub for updates, update in place from the `main` branch without git installed, and roll back to the previous files.
- An update keeps `.env`, your `config.yaml` values, `logs/`, `state/` and `.venv/`. Replaced files are copied to `backups/<timestamp>/` first.
- Updating is refused while the bot is running with an open position or a pending order.
- If `requirements.txt` changes, the dashboard runs `pip install -r requirements.txt` with the same Python and shows the output.
