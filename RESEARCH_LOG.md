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

## Round 2: our OBI-MR strategy (pine/obi_mr_xauusd_v2.pine), 1h, 2026-11-26

| Test | Result | Pass/Fail |
|---|---|---|
| Baseline (default params), discovery | PF 0.94 at realistic cost (1.02 gross), 394 trades | Fail |
| Optimised on discovery (EMA192, stretch 2 ATR, flow z ≥ 1.5, Asia session, 3 ATR stop, 24 h hold) | PF 1.59-1.65 at realistic cost, 152 trades, every year > 1 | Pass |
| Walk-forward (4 yearly folds inside discovery) | Out-of-sample PF 1.67, 110 trades, 3/4 folds positive, WF efficiency 0.98 | Pass |
| **30% holdout 2023-11 → 2026-05** (params frozen in `results_xauusd_tk_freeze.json` before the run) | **PF 1.02** at realistic cost (1.08 gross), 74 trades, +0.2%; PF 0.70 without its best 3 trades | **Fail** |
| Monte Carlo (1800 block bootstrap, holdout) | P(return < 0) = 51%; 5th-95th pct return -9% to +12% | **Fail** |
| Cross-asset, frozen params (XAGUSD / EURUSD / GBPUSD) | Negative gross P&L on all three | **Fail** |
| ±25% sweep | Not run: the holdout had already failed, and sweeping on the holdout would be snooping | n/a |

**Verdict: REJECTED.** The in-sample edge (Asia-session fade of stretched moves
with strong opposing flow) did not survive 2024-26. That period is gold's
strong bull trend: 58 of the 74 holdout trades were shorts fading the rally.

**Account warning:** the TradingKit account showed 10 strategies this session
did not create ("oddslab …" crypto 5m, "probe_rsi"). They appeared during the
run and were left untouched. The API key had been pasted in chat; rotate it.

## Round 3: OBI-MR v3, trend-filtered (pre-registered in PREREG_v3.md), 2026-11-27

Development data: 2018-01 to 2026-05, 16 combos.

* **The trend filter destroys the trade count.** EMA600: 11-49 trades in 8.4
  years. EMA1200/2400 versions lose money.
* **Best overall is still the unfiltered Asia / flow ≥ 1.5 version:** PF 1.36
  at realistic cost, 226 trades, about +2.5%/yr.

Walk-forward, each year chosen from prior years only:

| Year | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|---|
| PF | 1.97 | 2.07 | 1.21 | 1.49 | **0.47** | **0.99** | no trades |

Gates:
* Stitched PF 1.33: pass.
* 4/7 folds positive: pass.
* **2024-26 combined PF 0.89: fail.**

**Verdict: REJECTED.** The 2010-2017 fresh holdout was **not** used and
remains clean for a future hypothesis.

Conclusion across rounds 1-3: 1h price-stretch mean reversion on XAUUSD
earned money in 2018-2023 and stops working in the 2024-26 one-way bull
market. A trend filter cannot rescue it without removing almost all trades.

## Round 4: published trader systems (pre-registered in PREREG_traders.md), 2026-11-27

Development data: 2018-01 → 2026-05. Realistic cost is 0.02% round trip.
Buy & hold over the same period: about +248%.

| System | Trades | PF @0.02% | 2018-23 | 2024-26 | Gate |
|---|---|---|---|---|---|
| A Raschke Holy Grail (4h) | 15 | about 0.4 | - | - | Fail (trades) |
| B Turtle System 2 (1D) | 33 | about 1.2 | - | - | Fail (trades; 10/11 shorts lost) |
| **C1 TSMOM 63d (1D)** | **120** | **2.07** | **1.52 (+26%)** | **3.11 (+70%)** | **Pass** |
| C2 TSMOM 252d (1D) | 58 | high | - | - | Fail (trades; 95% of profit from one open trade) |
| D London Judas swing (15m) | 1256 | 0.83 | 0.91 | 0.65 | Fail (no gross edge) |

C1 detail:
* Longs PF 4.70, shorts PF 0.54.
* PF without its best trade: 1.74.

**C1 fresh holdout, 2010-2017, run once:**
* **Data defect:** TradingKit's daily XAUUSD is missing about 2.5 years
  (mid-2010 to mid-2013). Trade #9 spans the gap and is excluded, as is
  warm-up trade #1.
* 74 valid trades: PF 1.13 at 0.02% round trip (longs 1.21, shorts 1.06),
  +5.3%, max DD -26%.
* Monte Carlo block bootstrap: P(return < 0) = 43%.
* **Result: fails** the holdout rule (PF > 1.2, P(loss) ≤ 10%).

**Verdict:** no system passed every pre-registered test. C1 (3-month
time-series momentum) is the only one above break-even in all three periods
(2010-17 PF 1.13, 2018-23 PF 1.52, 2024-26 PF 3.11). It is a **research
candidate, not live-ready**: its edge is weak outside strong trends,
drawdowns run about 25%, and the short side adds little.

Sources:
* Raschke & Connors, *Street Smarts* (1995)
* Turtle System 2 rules
* Moskowitz, Ooi & Pedersen, "Time Series Momentum", JFE 2012
* ICT-style London sweep write-ups (vendor and retail; no performance data)
