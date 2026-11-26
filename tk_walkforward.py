#!/usr/bin/env python3
"""Walk-forward over pre-run TradingKit grids (discovery period only).

Each grid combo was run once over the discovery window; indicators are causal,
so a combo's trades in year Y equal a run that starts earlier and ends in Y.
Fold k: pick the combo with the best realistic-cost PF on train years
(2018..Y-1, min trades), then score it on unseen year Y.

    python tk_walkforward.py tk_runs/wf
"""
import glob, os, sys
import numpy as np, pandas as pd
import tk_analyze as T

COST, MIN_TRAIN = T.REAL_COST, 30
FOLDS = [(2020, "2020"), (2021, "2021"), (2022, "2022"), (2023, "2023 to Oct")]


def pf(r):
    w, l = r[r > 0].sum(), -r[r <= 0].sum()
    return w / l if l > 0 else float("inf")


def main(d):
    combos = {os.path.basename(p)[:-5]: T.load_trades(p) for p in sorted(glob.glob(f"{d}/*.json"))}
    for t in combos.values():
        t["r"] = t.gross_ret - COST
        t["year"] = t.entry_ts.dt.year
        t.drop(t[t.entry_ts >= pd.Timestamp(T.SPLIT, tz="UTC")].index, inplace=True)  # discovery only
    rows, oos = [], []
    for y, label in FOLDS:
        train = {k: t[t.year < y] for k, t in combos.items()}
        elig = {k: pf(v.r.values) for k, v in train.items() if len(v) >= MIN_TRAIN}
        best = max(elig, key=elig.get)
        test = combos[best][combos[best].year == y]
        oos.append(test.r.values)
        rows.append({"fold": label, "chosen": best, "train_pf": elig[best], "train_n": len(train[best]),
                     "test_pf": pf(test.r.values), "test_n": len(test), "test_avg_bp": test.r.mean() * 1e4,
                     "test_ret": np.prod(1 + test.r.values) - 1})
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(df.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    allr = np.concatenate(oos)
    print(f"\nStitched OOS @0.02%: trades {len(allr)}, PF {pf(allr):.2f}, avg {allr.mean()*1e4:.1f} bp, "
          f"return {np.prod(1+allr)-1:.2%}, positive folds {(df.test_ret > 0).mean():.0%}, "
          f"WF efficiency (mean test PF / mean train PF) {df.test_pf.mean()/df.train_pf.mean():.2f}")
    print("\nPer-combo full-discovery PF @0.02%:", ", ".join(f"{k}:{pf(t.r.values):.2f}({len(t)})" for k, t in combos.items()))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "tk_runs/wf")
