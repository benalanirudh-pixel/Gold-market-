# XAUUSD strategy research log (TradingKit / trader.dev)

Run on 2026-11-26. Data: TradingKit's XAUUSD feed ("POLYGON forex"), 2018-01-01 to 2026-05-19.

## Pre-registered protocol (fixed before any run)

* **One run per candidate.** Each candidate ran once over the full window,
  with its parameters fixed before the run.
* **Discovery / holdout split** (done locally with `tk_analyze.py`):
  * Discovery: trades entered before 2023-11-01. Used for ranking and selection.
  * Holdout: trades entered on or after 2023-11-01. Reserved for finalists only.
* **Costs:** TradingKit forces 0.05% commission per side (0.10% round trip),
  about 5-8x real XAUUSD costs. Every trade was re-scored from its gross P&L
  at:
  * a realistic **0.02% round trip** (spread + slippage + commission)
  * a 0.04% stress level
* **Gate to the holdout:** discovery profit factor **>= 1.4** at realistic
  cost, with **>= 100 trades**.
* **Finalist tests** (only if a candidate passed the gate): holdout profit
  factor > 1.2, Monte Carlo (1800 runs, iid + block bootstrap) 5th-percentile
  return > 0, ±25% parameter sweep, and XAGUSD cross-check.

## Results (discovery period only; the holdout was never opened)

| # | Candidate | TF | Profit factor, gross | Profit factor, realistic cost | Trades | Notes |
|---|---|---|---|---|---|---|
| 1 | Stretch reversion: price ≥ 2 ATR from EMA64, flow proxy z ≥ 0.5, ADX < 30; exit at EMA, 2 ATR stop or 24 bars | 15m | 1.02 | 0.86 | 1543 | Below 1 every year |
| 2 | Bollinger(20,2) + RSI(2) < 10 / > 90, with EMA200 trend filter | 15m | 1.11 | 0.90 | 1719 | |
| 3 | Asian range (00-07 UTC) breakout 07-12 UTC; stop at range midpoint, 1.5R target | 15m | 0.86 | 0.77 | 1117 | |
| 4 | Donchian 55 breakout with EMA200 filter, 3 ATR ratchet stop | 1h | 1.03 | 0.97 | 521 | Hit a bad tick, see below |
| 5a | Overnight long 22:00 → 07:00 UTC, no Friday entries | 1h | **1.19** | 1.03 | 917 | Real but tiny drift (+2.4 bp/night gross) |
| 5b | US-session short 13:00 → 20:00 UTC | 1h | 0.96 | 0.87 | 1411 | |
| 6 | RSI(2) dip on 4h with EMA200 filter, exit above SMA5 | 4h | 0.91 | 0.83 | 407 | |
| 7 | 5a, only above a ~50-day EMA | 1h | 0.99 | 0.85 | 438 | The filter made it worse |
| 8 | Daily SMA100 trend, long/short flip | 1D | 1.25 | **1.21** | 87 | Longs PF 2.14, shorts 0.51: mostly gold's beta |

**Verdict: REJECTED.** No candidate reached the discovery gate, so none was
evaluated on the holdout, sweep, cross-asset test or Monte Carlo. The holdout
(2023-11-01 to 2026-05-19) is still unused and clean.

## TradingKit findings

1. **The leaderboard is dominated by a backtest artifact.** The top XAUUSD
   strategies (profit factor ~5, +19,000%) use `trail_points = trail_offset =
   0.02 x ATR`. That is a trailing stop at about one spread, filled at the
   best price inside each candle. TradingKit itself warns that this "trails at
   ~zero distance". Same strategy, same period:

   | Trailing stop | Profit factor | Return |
   |---|---|---|
   | 0.02 ATR (as published) | 2.98 | +866% |
   | 0.5 ATR | 0.80 | -59% |
   | 1.0 ATR | 0.69 | -83% |

2. **Bad data point.** On 2025-11-23 22:00 UTC the feed prints 1964.99 while
   gold was about 4030. Candidate 4 shorted it and was wiped out. Check
   extreme trades before trusting any result.
3. **Fixed broker settings:** forced 0.05% commission per side and 100%-equity
   sizing; orders fill at the signal bar's close (`process_orders_on_close`).
4. **Every run is published.** Each `quick_backtest` auto-saves a **public**
   strategy, named after it, even without a name. All of this session's runs
   were deleted afterwards; their result pages may still be reachable by URL.

## Reproduce

```bash
# Save get_trades output to tk_runs/<name>.json, then:
python tk_analyze.py tk_runs/<name>.json                   # discovery only
python tk_analyze.py tk_runs/<name>.json --segment holdout --mc 1800   # finalists only
```
