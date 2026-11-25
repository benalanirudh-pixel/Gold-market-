# OBI + stretch mean reversion (BTC 15m), with validation harness

Single-file research strategy: `obi_meanrev.py`.

**Thesis.** When BTC is stretched far from a moving EMA anchor *and* the
resting order book leans against the move (bids heavy after a drop, asks heavy
after a rally), the move is liquidity noise and should revert to the anchor.

**Status: the edge does not survive validation.** See
[`results/REPORT.md`](results/REPORT.md) for every number; every test that
fails is reported as `EDGE DIES`.

## What is in the file

| Part | Function(s) |
|---|---|
| Data loader | `load_symbol` pulls Binance USD-M 15m klines and the `bookDepth` order-book snapshots (about 30 per bar, cumulative bid/ask notional within +/-1..5% of mid) from `data.binance.vision`, then caches them in `data/` |
| Signal logic | `indicators`: `stretch_z` = log(close/EMA) / rolling std; `obi_z` = z-score of the 1% book imbalance `(bid-ask)/(bid+ask)` |
| Position sizing | `position_size`: vol targeting (30% annualised), capped at 2x |
| Backtest | `backtest` + `metrics`: decide at bar close, fill at next open, 5 bp fee + 2 bp slippage per side; reports return, trades, win rate, Sharpe, max drawdown, PF, exposure |
| Validation | `harness`: walk-forward (5 expanding folds), 30% holdout, +/-25% full-grid parameter sweep (3^8 = 6561 runs), cross-asset (ETH, SOL, XRP), 1800-run trade bootstrap Monte Carlo |

Defaults, the optimisation grid and every pass threshold are set at the top
of the file (`Params`, `OPT_GRID`, `PASS`). They were fixed before any results
were seen and were not changed afterwards.

## Run

```bash
pip install -r requirements.txt        # numpy, pandas
python obi_meanrev.py --backtest-only  # one BTC backtest with defaults
python obi_meanrev.py                  # full harness -> results/REPORT.md
```

The first run downloads about 1.2 GB of public zips (about 4 minutes), keeps
only the 15m aggregates (about 2.5 MB per asset) and needs network access to
`data.binance.vision`.

## Results (full detail in `results/REPORT.md`)

| Test | Result |
|---|---|
| Walk-forward, 5 folds | Stitched OOS Sharpe -3.45, 1 of 5 folds positive: **EDGE DIES** |
| 30% holdout | -27.0% return, Sharpe -3.68, 193 trades, 37% win rate: **EDGE DIES** |
| +/-25% sweep, 6561 combos | 0% of the grid has Sharpe > 0, in-sample or holdout: **EDGE DIES** |
| ETH / SOL / XRP | Sharpe -2.98 / -2.44 / -2.35: **EDGE DIES** on all three |
| Monte Carlo, 1800 runs | 5th-95th pct return -39% to -13%, P(loss) 99.9%: **EDGE DIES** |

**Why it fails.** The edge does not just fail out of sample; there is no
edge in-sample either.

* **Gross P&L is about zero.** Before costs, BTC full-period P&L is -10%
  with Sharpe -0.16. Costs of 14 bp per round trip on about 1,200 trades take
  that to -80%.
* **Stretch shows momentum, not reversion.** Its correlation with forward
  1-16 bar returns is positive (+0.003 to +0.013). After a +2-sigma stretch,
  the next 4 hours average +9 bp, so price keeps going.
* **OBI adds almost nothing.** The imbalance z-score's correlation with
  forward returns is below 0.004 in absolute value.

**Integrity checks.**

* **Look-ahead test:** replacing all prices and book data after bar 30,000
  with random noise leaves every earlier trade and bar return unchanged.
* **Cost test:** a flat-price round trip loses exactly 2 x 7 bp x leverage.
* **No rescue tuning:** the signal direction was not flipped to momentum and
  no parameters were tuned after seeing results. Doing either would be
  in-sample fitting.

## Caveats

* Period: 2024-01-01 to 2025-10-31. Binance `bookDepth` history is only
  public for recent years, so this is a single market regime of about 22
  months.
* The book snapshot is Binance-perp only. It is not consolidated depth across
  venues.
* Funding payments are not modelled. Holds are short (8 h maximum) and
  funding is small relative to the costs that already kill the edge.
