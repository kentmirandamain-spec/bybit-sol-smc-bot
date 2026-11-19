# Changelog

## Unreleased

- Breakeven stop: after a configurable open profit (default off), the stop moves to entry plus fees. Optional ATR or 15m-swing trail after +1.5R. Dry-run, backtest, and live (Bybit `set_trading_stop`) share the rule.
- Partial take-profit: close a configurable percent (default off) at the first target and leave the rest for the final target. The split is skipped when either piece would be below the exchange minimum. Risk stays capped at 5 USDT.

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
