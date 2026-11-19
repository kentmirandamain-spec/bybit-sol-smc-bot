# Changelog

## 1.0.0 - 2026-11-18

- Initial release of the Bybit SOLUSDT Smart Money Concepts bot and the local Grok Bot dashboard.
- Overview can show a TradingView chart of BYBIT:SOLUSDT.P (Asia/Manila, entry-timeframe interval). Entry, stop and target stay listed beside it, and the built-in chart is one toggle away.
- The header uses the Grok mark, inlined so the logo works offline.
- The dashboard can check GitHub for updates, update in place from the `main` branch without git installed, and roll back to the previous files.
- An update keeps `.env`, your `config.yaml` values, `logs/`, `state/` and `.venv/`. Replaced files are copied to `backups/<timestamp>/` first.
- Updating is refused while the bot is running with an open position or a pending order.
- If `requirements.txt` changes, the dashboard runs `pip install -r requirements.txt` with the same Python and shows the output.
