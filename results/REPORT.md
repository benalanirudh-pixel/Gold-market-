# OBI + stretch mean reversion: validation report

Generated 2026-11-25 17:58 UTC by `obi_meanrev.py`.

* Data: Binance USD-M perpetuals, 15m bars, 2024-01-01 00:00:00 to 2025-10-31 23:45:00 (64320 BTC bars). OBI coverage: 99.7% of bars.
* Costs: 5.0 bp fee + 2.0 bp slippage per side (14 bp round trip, scaled by leverage).
* Sizing: vol target 30% annualised, max leverage 2.0x. Fill at next bar open.
* In-sample: bars 400 to 45024 (to 2025-04-14 00:00:00). Holdout (30%): 2025-04-14 00:00:00 to 2025-10-31 23:45:00.
* Default params: `{"anchor_len": 96, "z_len": 96, "obi_len": 96, "entry_z": 2.0, "obi_min": 0.5, "exit_z": 0.0, "stop_z": 4.0, "max_hold": 32}`

## 0. Reference backtest: BTC, default params, full period

| Segment | Return | Trades | Win rate | Sharpe | Max DD | Avg trade | PF | Exposure | Buy&hold |
|---|---|---|---|---|---|---|---|---|---|
| In-sample | -68.18% | 932 | 43.78% | -3.87 | -70.01% | -0.12% | 0.69 | 38.15% | 92.58% |
| In-sample (zero cost) | -9.09% | 932 | 48.28% | -0.22 | -23.11% | -0.01% | 0.98 | 38.15% | 92.58% |
| Holdout | -37.08% | 284 | 42.25% | -4.30 | -40.90% | -0.16% | 0.59 | 29.11% | 30.87% |
| Holdout (zero cost) | -0.94% | 284 | 51.41% | 0.00 | -13.51% | -0.00% | 1.00 | 29.11% | 30.87% |
| Full | -79.98% | 1216 | 43.42% | -3.97 | -81.02% | -0.13% | 0.67 | 35.42% | 152.03% |
| Full (zero cost) | -9.95% | 1216 | 49.01% | -0.16 | -23.11% | -0.01% | 0.98 | 35.42% | 152.03% |

Zero-cost rows are diagnostics only: they show whether any gross edge exists before fees.

## 1. Walk-forward (expanding window, inside the first 70%)

| Fold | Test window | Chosen on train | Train Sharpe | Return | Trades | Win rate | Sharpe | Max DD |
|---|---|---|---|---|---|---|---|---|
| 1 | 2024-03-22 to 2024-06-08 | anchor=96 entry=2.0 obi=1.0 | -0.97 | -11.38% | 125 | 50.40% | -2.54 | -17.08% |
| 2 | 2024-06-08 to 2024-08-24 | anchor=96 entry=2.5 obi=1.0 | -1.17 | -25.54% | 99 | 31.31% | -7.65 | -26.24% |
| 3 | 2024-08-24 to 2024-11-10 | anchor=96 entry=2.0 obi=1.0 | -3.11 | -15.61% | 145 | 42.76% | -3.63 | -18.27% |
| 4 | 2024-11-10 to 2025-01-26 | anchor=96 entry=2.0 obi=1.0 | -3.24 | 1.13% | 130 | 51.54% | 0.36 | -8.10% |
| 5 | 2025-01-26 to 2025-04-13 | anchor=96 entry=2.5 obi=1.0 | -2.37 | -17.67% | 111 | 36.04% | -4.43 | -19.24% |

Stitched OOS: return -53.63%, Sharpe -3.45, max DD -55.25%, trades 610, positive folds 20%. Mean train Sharpe -2.17 vs mean test Sharpe -3.58.

## 2. 30% out-of-sample holdout

Params fit on the full in-sample (best IS Sharpe on the 27-point grid): `{"anchor_len": 96, "z_len": 96, "obi_len": 96, "entry_z": 2.5, "obi_min": 1.0, "exit_z": 0.0, "stop_z": 4.0, "max_hold": 32}`

| Segment | Return | Trades | Win rate | Sharpe | Max DD | Avg trade | PF | Exposure | Buy&hold |
|---|---|---|---|---|---|---|---|---|---|
| In-sample (fit) | -49.38% | 648 | 41.36% | -2.74 | -53.41% | -0.10% | 0.73 | 25.58% | 92.58% |
| Holdout | -26.97% | 193 | 37.31% | -3.68 | -30.78% | -0.16% | 0.58 | 17.85% | 30.87% |

## 3. Parameter sweep (every parameter at -25% / 0 / +25%, full grid)

6561 combinations (33s). Zero-valued params were swept +/-0.25 absolute. Full grid in `results/sweep.csv`.

| Segment | Share Sharpe>0 | Share return>0 | Median Sharpe | 10th pct | 90th pct |
|---|---|---|---|---|---|
| In-sample | 0.00% | 0.00% | -3.95 | -7.55 | -2.67 |
| Holdout | 0.00% | 0.00% | -4.15 | -8.19 | -2.31 |

One-at-a-time (others at fitted values):

| Param | Value | IS Sharpe | Holdout Sharpe | Holdout trades |
|---|---|---|---|---|
| anchor_len | 72 | -3.24 | -3.57 | 168 |
| anchor_len | 96 | -2.74 | -3.68 | 193 |
| anchor_len | 120 | -2.91 | -4.16 | 222 |
| z_len | 72 | -3.95 | -3.85 | 244 |
| z_len | 96 | -2.74 | -3.68 | 193 |
| z_len | 120 | -3.07 | -3.74 | 160 |
| obi_len | 72 | -2.85 | -3.72 | 195 |
| obi_len | 96 | -2.74 | -3.68 | 193 |
| obi_len | 120 | -3.16 | -3.89 | 187 |
| entry_z | 1.875 | -3.36 | -3.34 | 261 |
| entry_z | 2.5 | -2.74 | -3.68 | 193 |
| entry_z | 3.125 | -2.78 | -4.66 | 132 |
| obi_min | 0.75 | -3.80 | -4.12 | 216 |
| obi_min | 1.0 | -2.74 | -3.68 | 193 |
| obi_min | 1.25 | -2.30 | -3.55 | 159 |
| exit_z | -0.25 | -2.55 | -3.42 | 193 |
| exit_z | 0.0 | -2.74 | -3.68 | 193 |
| exit_z | 0.25 | -3.00 | -3.70 | 194 |
| stop_z | 3.0 | -6.96 | -7.04 | 327 |
| stop_z | 4.0 | -2.74 | -3.68 | 193 |
| stop_z | 5.0 | -2.56 | -2.65 | 138 |
| max_hold | 24 | -2.88 | -3.76 | 203 |
| max_hold | 32 | -2.74 | -3.68 | 193 |
| max_hold | 40 | -3.13 | -2.99 | 191 |

## 4. Cross-asset (BTC-fitted params frozen)

| Asset | Segment | Return | Trades | Win rate | Sharpe | Max DD | Avg trade | Buy&hold | OBI cov |
|---|---|---|---|---|---|---|---|---|---|
| ETHUSDT | Full | -60.28% | 743 | 39.43% | -2.98 | -60.99% | -0.12% | 71.49% | 99.84% |
| ETHUSDT | Holdout | -20.34% | 193 | 35.75% | -2.82 | -25.71% | -0.12% | 140.86% | 99.84% |
| SOLUSDT | Full | -50.99% | 704 | 41.19% | -2.44 | -51.21% | -0.10% | 85.30% | 99.69% |
| SOLUSDT | Holdout | -18.80% | 161 | 40.37% | -2.64 | -20.80% | -0.13% | 45.85% | 99.69% |
| XRPUSDT | Full | -48.99% | 656 | 42.38% | -2.35 | -53.16% | -0.10% | 335.43% | 99.84% |
| XRPUSDT | Holdout | -17.94% | 160 | 42.50% | -2.41 | -21.92% | -0.12% | 18.41% | 99.84% |

## 5. Monte Carlo: 1800 bootstrap resamples of holdout trades

193 holdout trades resampled with replacement, 1800 runs.

| Percentile | Return | Sharpe (trade) | Max DD | Win rate |
|---|---|---|---|---|
| 5th | -38.98% | -6.27 | -40.01% | 31.61% |
| 25th | -32.32% | -4.97 | -33.62% | 35.23% |
| 50th | -27.06% | -4.05 | -28.78% | 37.31% |
| 75th | -21.66% | -3.13 | -24.00% | 39.38% |
| 95th | -13.20% | -1.76 | -16.48% | 43.01% |

P(return <= 0) = 99.9%; P(max DD worse than -20%) = 88.6%.

## Verdict

| Test | Value | Pass rule | Result |
|---|---|---|---|
| Walk-forward stitched OOS Sharpe | -3.45 | > 0.0 | EDGE DIES |
| Walk-forward positive folds | 20% | >= 60% | EDGE DIES |
| Holdout Sharpe | -3.68 | > 0.0 | EDGE DIES |
| Holdout return | -26.97% | > 0 | EDGE DIES |
| Sweep: share of +/-25% grid with holdout Sharpe > 0 | 0% | >= 70% | EDGE DIES |
| Cross-asset ETHUSDT full-period Sharpe | -2.98 | > 0.0 | EDGE DIES |
| Cross-asset SOLUSDT full-period Sharpe | -2.44 | > 0.0 | EDGE DIES |
| Cross-asset XRPUSDT full-period Sharpe | -2.35 | > 0.0 | EDGE DIES |
| Monte Carlo 5th-percentile return | -38.98% | > 0% | EDGE DIES |

**The edge dies in 9 of 9 tests:** Walk-forward stitched OOS Sharpe; Walk-forward positive folds; Holdout Sharpe; Holdout return; Sweep: share of +/-25% grid with holdout Sharpe > 0; Cross-asset ETHUSDT full-period Sharpe; Cross-asset SOLUSDT full-period Sharpe; Cross-asset XRPUSDT full-period Sharpe; Monte Carlo 5th-percentile return.
