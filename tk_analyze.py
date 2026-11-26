#!/usr/bin/env python3
"""
Re-score a TradingKit (trader.dev) trade list with realistic XAUUSD costs and a
pre-registered discovery / holdout split.

TradingKit forces 0.05% commission per side (0.10% round trip), about 5-8x a
realistic XAUUSD cost. This tool regroups the engine's fill rows into logical
trades, takes each trade's GROSS return (no commission), and subtracts a cost
you choose.

    python tk_analyze.py trades.json                        # discovery only
    python tk_analyze.py trades.json --segment holdout      # finalists only
    python tk_analyze.py trades.json --segment holdout --mc 1800

Holdout discipline: rank and choose using --segment discovery only. Look at
the holdout once, for finalists, after choosing.
"""
from __future__ import annotations

import argparse
import json
import math

import numpy as np
import pandas as pd

SPLIT = "2023-11-01"          # pre-registered: entries >= this are holdout
REAL_COST = 0.0002            # 0.02% round trip (spread + slippage + commission)


def load_trades(path: str) -> pd.DataFrame:
    rows = json.load(open(path))
    if isinstance(rows, dict):
        rows = rows.get("trades", rows)
    df = pd.DataFrame([r for r in rows if not r.get("isOpen")])
    # the engine splits one logical trade into several fill rows: regroup
    g = df.groupby(["entryTime", "entryId"], sort=True)
    t = pd.DataFrame({
        "side": g["direction"].first(),
        "entry_px": g["entryPrice"].first(),
        "qty": g["qty"].sum(),
        "gross": g["grossProfit"].sum(),
        "tk_net": g["profit"].sum(),
        "exit_time": g["exitTime"].max(),
        "bars": g["barsInTrade"].max(),
    }).reset_index()
    t["entry_ts"] = pd.to_datetime(t["entryTime"], unit="ms", utc=True)
    t["gross_ret"] = t["gross"] / (t["qty"] * t["entry_px"])
    t["tk_ret"] = t["tk_net"] / (t["qty"] * t["entry_px"])
    return t


def stats(r: np.ndarray) -> dict:
    if len(r) == 0:
        return {"trades": 0}
    w, l = r[r > 0], r[r <= 0]
    eq = np.cumprod(1 + r)
    curve = np.concatenate([[1.0], eq])
    dd = (curve / np.maximum.accumulate(curve) - 1).min()
    return {"trades": len(r), "win": len(w) / len(r),
            "pf": w.sum() / -l.sum() if l.sum() < 0 else float("inf"),
            "avg_bp": r.mean() * 1e4, "ret": eq[-1] - 1, "max_dd": dd,
            "avg_win_bp": w.mean() * 1e4 if len(w) else 0, "avg_loss_bp": l.mean() * 1e4 if len(l) else 0}


def block_mc(r: np.ndarray, runs: int, block: int, seed: int, dd_limit: float, ruin: float) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = len(r)
    out = []
    for method in ("iid", "block"):
        for _ in range(runs):
            if method == "iid":
                idx = rng.integers(0, n, n)
            else:
                nb = math.ceil(n / block)
                st = rng.integers(0, n, nb)
                idx = ((st[:, None] + np.arange(block)[None, :]) % n).ravel()[:n]
            s = r[idx]
            curve = np.concatenate([[1.0], np.cumprod(1 + s)])
            dd = (curve / np.maximum.accumulate(curve) - 1).min()
            streak = cur = 0
            for x in s <= 0:
                cur = cur + 1 if x else 0
                streak = max(streak, cur)
            out.append((method, curve[-1] - 1, dd, streak, curve.min() <= ruin))
    return pd.DataFrame(out, columns=["method", "ret", "max_dd", "lose_streak", "ruin"])


def report(t: pd.DataFrame, seg: str, costs: list[float]) -> pd.DataFrame:
    rows = []
    for c in costs:
        r = (t.gross_ret - c).values
        s = stats(r)
        rows.append({"segment": seg, "cost_rt_%": c * 100, **s})
    s = stats(t.tk_ret.values)
    rows.append({"segment": seg, "cost_rt_%": "TK 0.10", **s})
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--segment", choices=["discovery", "holdout", "both"], default="discovery")
    ap.add_argument("--split", default=SPLIT)
    ap.add_argument("--mc", type=int, default=0)
    ap.add_argument("--block", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20240611)
    ap.add_argument("--label", default="")
    a = ap.parse_args()
    t = load_trades(a.path)
    disc = t[t.entry_ts < pd.Timestamp(a.split, tz="UTC")]
    hold = t[t.entry_ts >= pd.Timestamp(a.split, tz="UTC")]
    costs = [0.0, REAL_COST, 2 * REAL_COST]
    segs = {"discovery": [("discovery", disc)], "holdout": [("holdout", hold)],
            "both": [("discovery", disc), ("holdout", hold)]}[a.segment]
    pd.set_option("display.width", 200)
    print(f"== {a.label} | logical trades: {len(t)} ({t.entry_ts.min():%Y-%m-%d} .. {t.entry_ts.max():%Y-%m-%d})")
    for name, seg in segs:
        rep = report(seg, name, costs)
        print(rep.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
        r = (seg.gross_ret - REAL_COST).values
        side = []
        for sd in ("long", "short"):
            ss = stats((seg[seg.side == sd].gross_ret - REAL_COST).values)
            side.append(f"{sd}: n={ss.get('trades', 0)} pf={ss.get('pf', float('nan')):.2f} avg={ss.get('avg_bp', float('nan')):.1f}bp")
        print("   " + " | ".join(side))
        yr = seg.assign(r=r).groupby(seg.entry_ts.dt.year)["r"].apply(lambda x: stats(x.values)["pf"])
        print("   PF by year @0.02%: " + ", ".join(f"{y}:{v:.2f}" for y, v in yr.items()))
        if a.mc and len(r) >= 10:
            mc = block_mc(r, a.mc, a.block, a.seed, 0.20, 0.5)
            for m, s in mc.groupby("method"):
                print(f"   MC {m:5s} x{a.mc}: median ret {s.ret.median():.2%}, 5th {s.ret.quantile(.05):.2%}, "
                      f"95th {s.ret.quantile(.95):.2%}, median DD {s.max_dd.median():.2%}, 95th-pct DD "
                      f"{s.max_dd.quantile(.05):.2%}, P(ret<0) {(s.ret < 0).mean():.1%}, "
                      f"P(DD>=20%) {(s.max_dd <= -0.20).mean():.1%}, P(ruin) {s.ruin.mean():.1%}, "
                      f"lose streak med/95% {s.lose_streak.median():.0f}/{s.lose_streak.quantile(.95):.0f}")


if __name__ == "__main__":
    main()
