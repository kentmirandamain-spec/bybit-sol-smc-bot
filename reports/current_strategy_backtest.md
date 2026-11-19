# Current strategy backtest

Shipped defaults from `config.yaml` on main v1.2.0. This run does not change that file.

- Symbol: **SOLUSDT** only. Higher timeframe 60m bias, entry timeframe 15m.
- Rules: higher-timeframe structure bias, 15m liquidity sweep, CHoCH required, order-block limit, target at **2R**. Breakeven, trail, and partial take-profit are off. News filter is off.
- Risk: **5 USDT** per trade, fees inside that risk. Daily loss cap **15 USDT**. At most **6** fills per UTC day. After **3** consecutive losses, no new entry for the rest of that UTC day. One position at a time.
- Data: **okx** public SOLUSDT candles, 2025-10-30 03:15 UTC → 2026-11-19 03:15 UTC (385 days including 20 days of warm-up that cannot count as a reported fill). 36960 entry bars, 9239 bias bars.
- Fees: maker 0.0002 on the limit entry, taker 0.00055 on the stop or target, same sizing as `backtest.py`. A win is a trade with positive R after those fees. **Slippage is not modelled. Funding is not modelled.**
- USDT P&L is net R times 5. Max drawdown in USDT is the R drawdown times 5. Profit factor is the sum of winning R divided by the absolute sum of losing R.
- The plain `backtest.py` replay does not apply the daily cap, the trade-count cap, or the loss-streak pause. This report does, because the bot shipped in v1.2.0 does. Closed signals before those guards: 143. After the guards: 143. On this sample the extra guards removed 0 trade(s); the strategy replay already allows only one position.

## Windows

| Window | Trades | Win rate | Avg R | Total R | P&L USDT | Max DD R / USDT | Losing streak | Profit factor | Long / short |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| last 30d (2026-10-20 03:15 UTC → 2026-11-19 03:15 UTC) | 9 | 44.4% | +0.236 | +2.12 | +10.61 | 3.34 / 16.71 | 3 | 1.39 | 4 (+1.61R) / 5 (+0.51R) |
| last 90d (2026-08-21 03:15 UTC → 2026-11-19 03:15 UTC) | 36 | 27.8% | -0.287 | -10.31 | -51.57 | 18.59 / 92.95 | 9 | 0.64 | 18 (-7.86R) / 18 (-2.45R) |
| last 180d (2026-05-23 03:15 UTC → 2026-11-19 03:15 UTC) | 76 | 31.6% | -0.169 | -12.88 | -64.39 | 27.70 / 138.50 | 11 | 0.78 | 37 (-7.89R) / 39 (-4.99R) |
| last 365d (2025-11-19 03:15 UTC → 2026-11-19 03:15 UTC) | 137 | 32.8% | -0.134 | -18.33 | -91.66 | 27.70 / 138.50 | 11 | 0.82 | 63 (-10.00R) / 74 (-8.33R) |

## Month by month (last 365 days)

Months are the UTC month of the fill. The first month starts on the first reported fill's window boundary, and the last month ends at the last candle, so those two months are partial.

| Month | Trades | Win rate | Total R |
|---|---:|---:|---:|
| 2025-11 | 6 | 0.0% | -6.85 |
| 2025-12 | 10 | 50.0% | +3.90 |
| 2026-01 | 10 | 20.0% | -5.34 |
| 2026-02 | 8 | 50.0% | +2.97 |
| 2026-03 | 9 | 44.4% | +1.98 |
| 2026-04 | 12 | 41.7% | +1.79 |
| 2026-05 | 10 | 10.0% | -8.38 |
| 2026-06 | 17 | 41.2% | +2.39 |
| 2026-07 | 13 | 53.8% | +6.21 |
| 2026-08 | 11 | 9.1% | -9.27 |
| 2026-09 | 15 | 6.7% | -13.75 |
| 2026-10 | 13 | 53.8% | +6.28 |
| 2026-11 | 3 | 33.3% | -0.26 |

Past results are one path of SOLUSDT. They are not a forecast.
