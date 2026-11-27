# Pre-registration: OBI-MR XAUUSD v3 (written before any v3 backtest)

Date: 2026-11-27. Strategy: `pine/obi_mr_xauusd_v3.pine` (v2 plus a trend filter).

**Hypothesis.** v2 failed on 2023-11 to 2026-05 because it faded a strong bull
trend (58 of 74 holdout trades were shorts). Fading stretched moves only in
the direction of the higher-timeframe trend should keep the reversion edge and
avoid fighting trends.

**Data.**
* Development: XAUUSD 1h, 2018-01-01 to 2026-05-19. This includes the old
  holdout, which is now **seen** data.
* Fresh holdout: **2010-01-01 to 2017-12-31**, never used before. It is run
  once, with frozen parameters.

**Grid (16 combos).**
* Varied: trendMode/trendLen in {off, 600, 1200, 2400}, sessMode in
  {all, Asia}, flowMin in {1.0, 1.5}.
* Fixed: EMA192 anchor, entryZ 2.0, stop 3 ATR, max hold 24, ADX ≤ 30,
  ATR ratio ≤ 1.5.

**Selection.** Year-by-year walk-forward with test years 2020-2026. Each fold
picks the best combo on prior years, by realistic-cost PF with ≥ 30 trades.
The final parameters are the best combo on all of 2018-2026.

**Gates.**
* Development walk-forward: stitched out-of-sample PF > 1.2 at 0.02% round
  trip; ≥ 4 of 7 folds positive; 2024-2026 folds combined PF > 1.0.
* Fresh holdout (2010-2017): PF > 1.2 at 0.02% round trip, return > 0,
  ≥ 30 trades, Monte Carlo block bootstrap P(return < 0) ≤ 10%.
* Then a ±25% one-at-a-time sweep and the XAGUSD check.

**Cost model.** Each trade's gross return minus 0.02% round trip; 0.04% stress.
