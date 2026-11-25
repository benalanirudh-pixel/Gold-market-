# Gold-market-: quantitative research

| Project | File | Status |
|---|---|---|
| **XAUUSD 15m mean reversion** (current) | `xauusd_meanrev.py` | Built and self-tested. **Not yet run on market data** (see "Data status") |
| BTC 15m OBI mean reversion (earlier) | `obi_meanrev.py` | Run on Binance data: **rejected**, failed 9 of 9 tests (`results/REPORT.md`) |

---

## XAUUSD 15m mean-reversion system

### Thesis

When XAUUSD is stretched far from a moving fair-value anchor, and the quote
book or order flow leans against the stretch, price should revert to the
anchor. "Leans against the stretch" means bids are building after a selloff,
or offers are building after a rally.

The harness tests this separately for longs and shorts, across sessions and
volatility regimes, and after realistic broker costs.

### What is in the single file

| # | Component | Where |
|---|---|---|
| 1 | Data loaders: generic bars, generic ticks, MT5 bar/tick exports (with DST-aware server clocks), Dukascopy tick downloader | `load_bars`, `download_dukascopy`, `ticks_to_bars` |
| 2 | Validation and cleaning: duplicates, misaligned timestamps, impossible OHLC, crossed quotes, gaps (not forward-filled), spikes (flagged, never deleted) | `validate_bars` |
| 3 | Tick / order-book processing: bid/ask OHLC, tick count, top-of-book size imbalance, tick-rule imbalance | `ticks_to_bars` |
| 4 | Indicators: anchors (EMA, SMA, rolling VWAP, VWMA, session VWAP); stretch in ATR, std, z-score or % units; ATR; ADX; ATR percentile; spread vs median; return autocorrelation | `FeatureStore`, `anchor_series`, `atr_adx` |
| 5 | Signals: long and short with separate enable flags and asymmetric multipliers; OBI z-score with optional "improving" check; ADX, volatility, spread, session and mean-reversion-confirmation filters | `backtest` (entry block) |
| 6 | Position sizing: fixed-fractional risk to the ATR stop, broker contract size, min lot / lot step / max lot, exposure cap. Lots are never fractional | `position_size` |
| 7 | Execution simulator: decide at close, fill next open at ask (buy) / bid (sell) + slippage; intrabar stops on the executable side with gap-through fills | `backtest` |
| 8 | Cost model: real bid/ask spread from data, slippage, commission per lot per side, swap per lot per night with triple-swap day, and a stress multiplier for each | `CostModel`, `InstrumentSpec` |
| 9 | Exits: anchor, ATR stop, max hold, trailing stop, signal invalidation, session end, max adverse excursion, flat before the weekend | `backtest` (exit block) |
| 10 | Risk engine: daily loss stop, drawdown halt, consecutive-loss pause, one position at a time, no entries in the 17:00 NY rollover window | `backtest` |
| 11 | Reporting: return, annualised return, trades, win rate, PF, average win/loss, expectancy ($ and R), Sharpe, Sortino, max DD, recovery factor, holding time, long vs short, monthly returns, gross vs net, cost breakdown | `metrics` |
| 12 | Walk-forward: 5 expanding folds inside the first 70%; parameters optimised on train only | `run_harness` §3 |
| 13 | 30% holdout: final 30%, frozen parameters, evaluated once, with a contamination guard (`holdout_lock.json`) | `holdout_guard` |
| 14 | ±25% sensitivity: full 3^6 grid over the OBI threshold, stretch threshold, ATR stop, anchor length, max hold and volatility filter; knife-edge detection | §6 |
| 15 | Cross-asset: XAGUSD, EURUSD, GBPUSD with XAUUSD parameters frozen and each instrument's own contract and costs | §8 |
| 16 | Monte Carlo: 1,800 runs for each of iid bootstrap, permutation and block bootstrap, plus a day-level block bootstrap for daily-loss breaches | `mc_simulate`, `mc_daily` |
| 17 | Equity, drawdown and monthly charts (PNG); CSV and JSON exports | `save_plots`, `results_xauusd/` |
| + | Look-ahead test (future data replaced by noise must not change the past), concentration test, research studies, self-test | `lookahead_check`, `selftest` |

All pass rules are fixed in `HarnessConfig.rules` before any result is seen.
The final classification is computed from them: REJECTED, RESEARCH
PROMISING, PAPER-TRADING READY or LIVE-CANDIDATE.

### Install and run

```bash
python3 --version                 # needs >= 3.10
pip install -r requirements.txt   # numpy, pandas, matplotlib (optional)

python xauusd_meanrev.py selftest                  # synthetic checks (~2 min)
python xauusd_meanrev.py write-config --out config.json
python xauusd_meanrev.py download --start 2021-01-01 --end 2025-10-31   # Dukascopy, or use your broker export
python xauusd_meanrev.py run --config config.json  # full harness -> results_xauusd/REPORT.md
python xauusd_meanrev.py backtest --config config.json                  # single backtest
```

### Dataset schema (format `bars`, UTC)

```
ts,bid_open,bid_high,bid_low,bid_close,ask_open,ask_high,ask_low,ask_close,tick_volume,l1_imb,tickdir_imb
2024-03-04 14:30:00+00:00,2115.12,2117.40,2114.85,2116.90,2115.40,2117.66,2115.12,2117.18,1834,0.081,-0.12
```

* `l1_imb`, `tickdir_imb` and `obi_L1..obi_Ln` (real multi-level depth) are
  optional.
* Bid-only data needs a `spread` column, or a constant fallback spread is used
  and the report flags the spread as ASSUMED.
* Other formats are also accepted: `ticks` (`ts,bid,ask[,bid_size,ask_size]`),
  `mt5_bars` and `mt5_ticks`. For MT5 files set `server_tz` to `NY+7`,
  `+02:00` or `UTC`.

### Order-book honesty

Spot XAUUSD trades OTC, so there is no central order book and no public
historical multi-level depth. Every report states the source it used:

| Source | What it is |
|---|---|
| `depth_Lk` | Real multi-level depth. Only used if you supply recorded depth |
| `l1_size` | Real top-of-book quote sizes from one venue (Dukascopy bid/ask volume). Not full depth |
| `tick_direction` | Proxy: tick-rule order flow |
| `clv_volume` | Proxy: close-location value x tick volume |

### Self-test (synthetic data: verifies the code, says nothing about XAUUSD)

| Synthetic case | Expected | Got |
|---|---|---|
| Pure random walk, random book sizes | REJECTED | REJECTED, failed 10 of 10 tests (holdout PF 0.91, zero-cost PF 1.04) |
| Random walk plus a planted reverting component, with the book leaning against it | Detects it | Passed every test (holdout PF 2.61) |

Also checked:

* **No look-ahead:** trades are identical after replacing future data with
  noise.
* **Lot sizing:** lots are never fractional and the minimum lot is enforced.
* **MT5 clock conversion:** a `NY+7` clock converts to UTC correctly in both
  winter (UTC+2) and summer (UTC+3).
* **Holdout guard:** it flags a holdout re-run with different parameters.

### Data status

Not run on real XAUUSD yet: no XAUUSD, XAGUSD, EURUSD or GBPUSD market data
could be obtained in the build session.

* **Dukascopy:** the data server reset every connection from that cloud
  environment, after one HTTP 429.
* **FXCM / HistData:** probing them was blocked in that session.
* **Downloader not verified on real files:** `download_dukascopy` follows the
  public `.bi5` format, but it was never run against real files. Check its
  first output (`data/bars/XAUUSD_15m.csv.gz`) before trusting it.

`dukascopy-node` was tried as well. It uses Dukascopy's other server,
`jetta.dukascopy.com`, but Dukascopy's firewall answered HTTP 403 to that
cloud IP too.

**Recommended route: dukascopy-node on your own machine** (Node.js 20+):

```bash
# A. 15m bid + ask candles: fast, small. OBI is then a PROXY (clv_volume).
for s in xauusd xagusd eurusd gbpusd; do for p in bid ask; do
  npx dukascopy-node -i $s -from 2021-01-01 -to 2025-10-31 -t m15 -p $p -f csv -dir data/raw
done; done

# B. Ticks: slow, several GB per symbol, but they carry real top-of-book bid/ask
#    sizes (OBI = l1_size). Use B for all four symbols or for none, so every
#    instrument uses the same OBI source.
for s in xauusd xagusd eurusd gbpusd; do
  npx dukascopy-node -i $s -from 2021-01-01 -to 2025-10-31 -t tick -f csv -dir data/raw
done
```

Then point `config.json` at the files. Use the file names dukascopy-node
printed. For route A:

```json
"XAUUSD": {"format": "dukascopy_node_bars",
           "path": "data/raw/xauusd-m15-bid-2021-01-01-2025-10-31.csv",
           "ask_path": "data/raw/xauusd-m15-ask-2021-01-01-2025-10-31.csv"}
```

For route B, use `{"format": "dukascopy_node_ticks", "path": "data/raw/xauusd-tick-....csv"}`.

Then run `python xauusd_meanrev.py run --config config.json`. An MT5 export
from your broker works too (`mt5_bars` / `mt5_ticks`).

### Limitations

* **Constant FX conversion:** quote-to-account conversion is a constant. All
  four instruments are USD-quoted, so this is exact for a USD account.
* **One position per instrument**, no portfolio-level simulation.
* **Contract specs are placeholders:** default swap, commission and slippage
  values are typical-ECN guesses. Replace them with your broker's.
* **MT5 bar spreads understate costs:** MT5 records the minimum spread of
  each bar.
* **Single-venue imbalance:** top-of-book size imbalance from one venue is not
  the whole market.
* **Not COMEX:** spot/CFD XAUUSD is a different instrument from COMEX GC
  futures. No futures roll logic is needed here, and none is claimed.
