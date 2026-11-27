# Pre-registration: published gold/trend trader systems (written before any run)

Date: 2026-11-27. Instrument: XAUUSD on TradingKit (Polygon feed). Every system
trades both long and short.

| Id | System | Source | TF | File |
|---|---|---|---|---|
| A | Holy Grail: ADX(14) > 30 and rising, pullback to EMA20, close-beyond trigger, stop under pullback, target 20-bar extreme, exit ADX < 20 / 30 bars | Raschke & Connors, *Street Smarts* (1995) | 4h | pine/A_holy_grail.pine |
| B | Turtle System 2: 55-day breakout, 20-day exit, 2N stop | R. Dennis, Turtle rules | 1D | pine/B_turtle_s2.pine |
| C1 | Time-series momentum, 63-day lookback | Moskowitz, Ooi & Pedersen (2012), JFE | 1D | pine/C_tsmom.pine |
| C2 | Time-series momentum, 252-day lookback | same | 1D | pine/C_tsmom.pine |
| D | London "Judas swing" Asian-range sweep reversal, 07-10 UTC, target opposite side, min 1R | ICT-style retail gold method | 15m | pine/D_judas_swing.pine |

Parameters are the published or default values above. **No tuning before the gate.**

**Development data:** 2018-01-01 to 2026-05-19.

**Development gate**, all at a realistic 0.02% round-trip cost:
* PF ≥ 1.3
* ≥ 60 trades
* PF > 1 in 2018-2023 **and** in 2024-2026
* Reported alongside buy & hold

**Fresh holdout (finalists only, run once):** 2010-01-01 to 2017-12-31. Pass
requires PF > 1.2 at 0.02% round trip, return > 0, ≥ 30 trades, and a Monte
Carlo block bootstrap (1800 runs) with P(return < 0) ≤ 10%.
