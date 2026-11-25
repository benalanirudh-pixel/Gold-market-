#!/usr/bin/env python3
"""
OBI-confirmed stretch mean reversion -- BTCUSDT perpetual, 15m bars.

Edge thesis
-----------
When price is stretched far from a moving anchor (EMA) AND the resting order
book leans *against* the stretch (bids heavy after a selloff, asks heavy after
a rally), the move is more likely liquidity-driven noise than information, so
price should revert toward the anchor.

    long  : stretch_z <= -entry_z  and  obi_z >= +obi_min
    short : stretch_z >= +entry_z  and  obi_z <= -obi_min
    exit  : stretch reverts to the anchor (|stretch_z| <= exit_z on the
            entry side), or stop (|stretch_z| >= stop_z), or time stop.

Data (all public, Binance USD-M futures archive, data.binance.vision)
---------------------------------------------------------------------
* 15m klines (monthly zips)
* bookDepth: ~1-minute order-book snapshots giving cumulative bid / ask
  notional within +/-1..5% of mid (daily zips). Order-book imbalance per bar
  = mean over that bar's snapshots of (bid1% - ask1%) / (bid1% + ask1%).

No look-ahead: every signal uses data available at the close of bar t and is
filled at the OPEN of bar t+1. Costs: 5 bp taker fee + 2 bp slippage per side.

Validation harness (run `python obi_meanrev.py`)
-----------------------------------------------
1. Walk-forward (expanding window, 5 folds) inside the first 70% of history.
2. 30% out-of-sample holdout, touched once with params fit on the first 70%.
3. Parameter sweep: every parameter at -25% / 0 / +25% (full 3^8 grid).
4. Cross-asset: ETH, SOL, XRP with the BTC parameters frozen.
5. Monte Carlo: 1800 bootstrap resamples of the holdout trade sequence.
Every test has a pre-declared pass threshold; failures are printed as
"EDGE DIES" and written to results/REPORT.md. Nothing is hidden.

Usage
-----
    python obi_meanrev.py                 # download (cached) + full harness
    python obi_meanrev.py --backtest-only # single BTC backtest with defaults
    python obi_meanrev.py --no-sweep      # skip the 6561-point sweep
"""
from __future__ import annotations

import argparse
import io
import itertools
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from multiprocessing import Pool

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Configuration (fixed BEFORE looking at any result)
# --------------------------------------------------------------------------
BASE_URL = "https://data.binance.vision/data/futures/um"
PRIMARY = "BTCUSDT"
CROSS_ASSETS = ["ETHUSDT", "SOLUSDT", "XRPUSDT"]
START = "2024-01-01"
END = "2025-10-31"            # inclusive; last full month of archive used
INTERVAL = "15m"
BARS_PER_YEAR = 4 * 24 * 365  # crypto trades 24/7
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

FEE_BPS = 5.0                 # Binance USD-M taker
SLIP_BPS = 2.0
COST_PER_SIDE = (FEE_BPS + SLIP_BPS) / 1e4

# Position sizing: volatility targeting, capped leverage
TARGET_ANN_VOL = 0.30         # annualised vol of the position while in a trade
VOL_LEN = 96                  # 1 day of 15m bars for realised vol
MAX_LEVERAGE = 2.0
TARGET_BAR_VOL = TARGET_ANN_VOL / math.sqrt(BARS_PER_YEAR)

HOLDOUT_FRAC = 0.30
WF_FOLDS = 5
WARMUP = 400                  # bars skipped so every indicator is populated
MC_RUNS = 1800
SEED = 7


@dataclass(frozen=True)
class Params:
    anchor_len: int = 96      # EMA anchor (1 day)
    z_len: int = 96           # window for stretch std
    obi_len: int = 96         # window for OBI z-score
    entry_z: float = 2.0      # stretch threshold to enter
    obi_min: float = 0.5      # OBI z confirmation
    exit_z: float = 0.0       # exit when stretch back within this of the anchor
    stop_z: float = 4.0       # stretch stop
    max_hold: int = 32        # time stop: 8 hours


INT_PARAMS = {"anchor_len", "z_len", "obi_len", "max_hold"}

# Walk-forward / IS optimisation grid (small on purpose: 27 combos)
OPT_GRID = {
    "anchor_len": [48, 96, 192],
    "entry_z": [1.5, 2.0, 2.5],
    "obi_min": [0.0, 0.5, 1.0],
}
MIN_TRADES_TO_SELECT = 20

# Pre-declared pass thresholds
PASS = {
    "wf_oos_sharpe_min": 0.0,       # stitched OOS Sharpe must exceed
    "wf_fold_pos_frac": 0.6,        # share of folds with positive return
    "holdout_sharpe_min": 0.0,
    "sweep_pos_frac": 0.70,         # share of sweep grid with Sharpe > 0 (holdout)
    "cross_sharpe_min": 0.0,        # per asset, full period, frozen params
    "mc_p5_return_min": 0.0,        # 5th pct of bootstrapped return must be > 0
}


# --------------------------------------------------------------------------
# Data loader
# --------------------------------------------------------------------------
def _fetch(url: str, retries: int = 4) -> bytes | None:
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except Exception as e:  # network hiccup
            err = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"failed to fetch {url}: {err}")


def _read_zip_csv(blob: bytes, header_probe: str) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        raw = z.read(z.namelist()[0])
    has_header = raw[:64].decode(errors="ignore").startswith(header_probe)
    return pd.read_csv(io.BytesIO(raw), header=0 if has_header else None)


KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
              "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume",
              "ignore"]


def _load_klines_month(sym: str, ym: str) -> pd.DataFrame | None:
    blob = _fetch(f"{BASE_URL}/monthly/klines/{sym}/{INTERVAL}/{sym}-{INTERVAL}-{ym}.zip")
    if blob is None:
        return None
    df = _read_zip_csv(blob, "open_time")
    df.columns = KLINE_COLS[: df.shape[1]]
    t = df["open_time"].astype("int64")
    unit = "us" if t.iloc[0] > 1e14 else "ms"
    df["ts"] = pd.to_datetime(t, unit=unit)
    return df[["ts", "open", "high", "low", "close", "volume"]]


def _load_depth_day(sym: str, day: str) -> pd.DataFrame | None:
    blob = _fetch(f"{BASE_URL}/daily/bookDepth/{sym}/{sym}-bookDepth-{day}.zip")
    if blob is None:
        return None
    df = _read_zip_csv(blob, "timestamp")
    df.columns = ["timestamp", "percentage", "depth", "notional"][: df.shape[1]]
    df = df[df["percentage"].isin([-1, 1, -5, 5])]
    wide = df.pivot_table(index="timestamp", columns="percentage",
                          values="notional", aggfunc="last")
    wide.index = pd.to_datetime(wide.index)
    out = pd.DataFrame(index=wide.index)
    out["imb1"] = (wide[-1] - wide[1]) / (wide[-1] + wide[1])
    out["imb5"] = (wide[-5] - wide[5]) / (wide[-5] + wide[5])
    # snapshot inside [open, open+15m) belongs to bar `open` -> known at its close
    out["bucket"] = out.index.floor("15min")
    g = out.groupby("bucket")
    agg = g[["imb1", "imb5"]].mean()
    agg["n_snap"] = g.size()
    return agg


def load_symbol(sym: str, start: str = START, end: str = END,
                refresh: bool = False) -> pd.DataFrame:
    """15m OHLCV + order-book imbalance for `sym`, cached to data/."""
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{sym}_{INTERVAL}_{start}_{end}.csv.gz")
    if os.path.exists(path) and not refresh:
        return pd.read_csv(path, parse_dates=["ts"], index_col="ts")

    months = pd.period_range(start, end, freq="M").strftime("%Y-%m").tolist()
    days = pd.date_range(start, end, freq="D").strftime("%Y-%m-%d").tolist()
    print(f"[data] {sym}: {len(months)} kline months, {len(days)} depth days ...",
          flush=True)
    with ThreadPoolExecutor(16) as ex:
        kl = list(ex.map(lambda m: _load_klines_month(sym, m), months))
        dp = list(ex.map(lambda d: _load_depth_day(sym, d), days))
    missing_k = [m for m, k in zip(months, kl) if k is None]
    missing_d = [d for d, x in zip(days, dp) if x is None]
    if missing_k:
        print(f"[data] {sym}: WARNING missing kline months {missing_k}")
    if missing_d:
        print(f"[data] {sym}: WARNING {len(missing_d)} missing depth days "
              f"(first: {missing_d[:5]})")
    k = pd.concat([x for x in kl if x is not None]).drop_duplicates("ts")
    k = k.set_index("ts").sort_index().astype(float)
    d = pd.concat([x for x in dp if x is not None])
    d = d[~d.index.duplicated(keep="last")]
    df = k.join(d, how="left")
    df = df.loc[start: pd.Timestamp(end) + pd.Timedelta(days=1)]
    df.to_csv(path, compression="gzip", index_label="ts")
    return df


# --------------------------------------------------------------------------
# Signal logic
# --------------------------------------------------------------------------
_IND_CACHE: dict = {}


def indicators(df: pd.DataFrame, p: Params, key: str = "") -> dict:
    """Causal features. Every value at index t uses data up to bar t's close."""
    ck = (key, p.anchor_len, p.z_len, p.obi_len)
    if key and ck in _IND_CACHE:
        return _IND_CACHE[ck]
    c = df["close"]
    anchor = c.ewm(span=p.anchor_len, adjust=False).mean()
    dev = np.log(c / anchor)
    stretch_z = dev / dev.rolling(p.z_len, min_periods=p.z_len // 2).std()
    obi = df["imb1"]
    mu = obi.rolling(p.obi_len, min_periods=p.obi_len // 2).mean()
    sd = obi.rolling(p.obi_len, min_periods=p.obi_len // 2).std()
    obi_z = (obi - mu) / sd
    vol = np.log(c).diff().rolling(VOL_LEN, min_periods=VOL_LEN // 2).std()
    out = {
        "open": df["open"].to_numpy(),
        "close": c.to_numpy(),
        "sz": stretch_z.to_numpy(),
        "oz": obi_z.to_numpy(),
        "vol": vol.to_numpy(),
    }
    out = {k: v.tolist() for k, v in out.items()}  # python lists: fast scalar loop
    if key:
        _IND_CACHE[ck] = out
    return out


# --------------------------------------------------------------------------
# Position sizing
# --------------------------------------------------------------------------
def position_size(bar_vol: float) -> float:
    """Fraction of equity (leverage) so the position runs at TARGET_ANN_VOL."""
    if not (bar_vol > 0):
        return 0.0
    return min(MAX_LEVERAGE, TARGET_BAR_VOL / bar_vol)


# --------------------------------------------------------------------------
# Backtest engine
# --------------------------------------------------------------------------
def backtest(ind: dict, p: Params, i0: int, i1: int,
             cost: float = COST_PER_SIDE) -> dict:
    """Event loop on bars [i0, i1). Decide at close t, fill at open t+1."""
    o, c, sz, oz, vol = ind["open"], ind["close"], ind["sz"], ind["oz"], ind["vol"]
    entry_z, obi_min, exit_z, stop_z = p.entry_z, p.obi_min, p.exit_z, p.stop_z
    max_hold = p.max_hold
    n = i1 - i0
    rets = [0.0] * n
    trades = []  # (entry_idx, exit_idx, side, lev, ret)
    eq = 1.0
    pos, lev, pending, pend_lev = 0, 0.0, None, 0.0
    entry_k, entry_eq = 0, 1.0
    for k in range(i0, i1):
        e0 = eq
        if pos != 0 and k > i0:                       # gap close[k-1] -> open[k]
            eq *= 1.0 + pos * lev * (o[k] / c[k - 1] - 1.0)
        if pending is not None:                       # fill at open[k]
            if pending == 0:
                eq *= 1.0 - cost * lev
                trades.append((entry_k, k, pos, lev, eq / entry_eq - 1.0))
                pos, lev = 0, 0.0
            else:
                entry_eq = eq
                pos, lev, entry_k = pending, pend_lev, k
                eq *= 1.0 - cost * lev
            pending = None
        if pos != 0:                                  # open[k] -> close[k]
            eq *= 1.0 + pos * lev * (c[k] / o[k] - 1.0)
        # ---- decisions at close[k] ----
        if k == i1 - 1:
            if pos != 0:                              # flatten at end of window
                eq *= 1.0 - cost * lev
                trades.append((entry_k, k, pos, lev, eq / entry_eq - 1.0))
                pos, lev = 0, 0.0
        elif pos == 0:
            s, b = sz[k], oz[k]
            if s <= -entry_z and b >= obi_min:
                pending, pend_lev = 1, position_size(vol[k])
            elif s >= entry_z and b <= -obi_min:
                pending, pend_lev = -1, position_size(vol[k])
            if pending is not None and pend_lev <= 0.0:
                pending = None
        else:
            s = sz[k]
            held = k - entry_k + 1
            if pos == 1:
                done = s >= -exit_z or s <= -stop_z
            else:
                done = s <= exit_z or s >= stop_z
            if done or held >= max_hold:
                pending = 0
        rets[k - i0] = eq / e0 - 1.0
    return {"rets": np.asarray(rets), "trades": trades}


def metrics(res: dict) -> dict:
    r = res["rets"]
    tr = np.array([t[4] for t in res["trades"]]) if res["trades"] else np.array([])
    eq = np.cumprod(1.0 + r)
    total = eq[-1] - 1.0 if len(eq) else 0.0
    sd = r.std()
    sharpe = r.mean() / sd * math.sqrt(BARS_PER_YEAR) if sd > 0 else 0.0
    peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))
    mdd = (np.concatenate([[1.0], eq]) / peak - 1.0).min()
    years = len(r) / BARS_PER_YEAR
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 and total > -1 else -1.0
    wins = tr[tr > 0]
    losses = tr[tr <= 0]
    exposure = (sum(t[1] - t[0] + 1 for t in res["trades"]) / len(r)) if len(r) else 0
    return {
        "return": total,
        "cagr": cagr,
        "trades": int(len(tr)),
        "win_rate": float(len(wins) / len(tr)) if len(tr) else float("nan"),
        "sharpe": sharpe,
        "max_dd": mdd,
        "avg_trade": float(tr.mean()) if len(tr) else float("nan"),
        "profit_factor": float(wins.sum() / -losses.sum()) if len(losses) and losses.sum() < 0 else float("nan"),
        "exposure": exposure,
    }


def buy_hold(ind: dict, i0: int, i1: int) -> float:
    return ind["close"][i1 - 1] / ind["open"][i0] - 1.0


# --------------------------------------------------------------------------
# Helpers for the harness
# --------------------------------------------------------------------------
def run(df, p, i0, i1, key, cost=COST_PER_SIDE):
    return backtest(indicators(df, p, key), p, i0, i1, cost)


def optimise(df, key, i0, i1, base: Params) -> tuple[Params, dict]:
    best, best_m = None, None
    for vals in itertools.product(*OPT_GRID.values()):
        p = replace(base, **dict(zip(OPT_GRID.keys(), vals)))
        m = metrics(run(df, p, i0, i1, key))
        if m["trades"] < MIN_TRADES_TO_SELECT:
            continue
        if best_m is None or m["sharpe"] > best_m["sharpe"]:
            best, best_m = p, m
    if best is None:                       # nothing traded enough: keep defaults
        best, best_m = base, metrics(run(df, base, i0, i1, key))
    return best, best_m


def sweep_values(name: str, v):
    if v == 0:                             # +/-25% of zero is zero: use +/-0.25 abs
        return [-0.25, 0.0, 0.25]
    vals = [v * 0.75, v, v * 1.25]
    if name in INT_PARAMS:
        vals = [max(2, int(round(x))) for x in vals]
    return vals


_SWEEP_CTX: dict = {}


def _sweep_worker(pdict):
    df, key, segs = _SWEEP_CTX["df"], _SWEEP_CTX["key"], _SWEEP_CTX["segs"]
    p = Params(**pdict)
    out = dict(pdict)
    for name, (i0, i1) in segs.items():
        m = metrics(run(df, p, i0, i1, key))
        out[f"{name}_sharpe"] = m["sharpe"]
        out[f"{name}_return"] = m["return"]
        out[f"{name}_trades"] = m["trades"]
    return out


def monte_carlo(trade_rets: np.ndarray, years: float, runs: int = MC_RUNS,
                seed: int = SEED) -> pd.DataFrame:
    """Bootstrap the trade sequence (with replacement), same trade count."""
    rng = np.random.default_rng(seed)
    n = len(trade_rets)
    rows = []
    tpy = n / years if years > 0 else n
    for _ in range(runs):
        s = trade_rets[rng.integers(0, n, n)]
        eq = np.cumprod(1 + s)
        peak = np.maximum.accumulate(np.concatenate([[1.0], eq]))
        mdd = (np.concatenate([[1.0], eq]) / peak - 1).min()
        sh = s.mean() / s.std() * math.sqrt(tpy) if s.std() > 0 else 0.0
        rows.append((eq[-1] - 1, sh, mdd, (s > 0).mean()))
    return pd.DataFrame(rows, columns=["return", "sharpe", "max_dd", "win_rate"])


def fmt(x, kind="f"):
    if isinstance(x, str):
        return x
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "n/a"
    if kind == "%":
        return f"{x * 100:.2f}%"
    if kind == "i":
        return f"{int(x)}"
    return f"{x:.2f}"


def md_table(rows: list[dict], cols: list[tuple[str, str, str]]) -> str:
    head = "| " + " | ".join(c[1] for c in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    body = ["| " + " | ".join(fmt(r.get(c[0]), c[2]) for c in cols) + " |" for r in rows]
    return "\n".join([head, sep, *body])


METRIC_COLS = [("return", "Return", "%"), ("trades", "Trades", "i"),
               ("win_rate", "Win rate", "%"), ("sharpe", "Sharpe", "f"),
               ("max_dd", "Max DD", "%"), ("avg_trade", "Avg trade", "%"),
               ("profit_factor", "PF", "f"), ("exposure", "Exposure", "%")]


# --------------------------------------------------------------------------
# Validation harness
# --------------------------------------------------------------------------
def harness(do_sweep: bool = True) -> int:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    rep: list[str] = []
    verdicts: list[dict] = []
    say = lambda s="": (print(s, flush=True), rep.append(s))  # noqa: E731

    def verdict(test, value, rule, ok):
        verdicts.append({"test": test, "value": value, "rule": rule,
                         "result": "PASS" if ok else "EDGE DIES"})
        print(f"  -> {test}: {value}  [{rule}]  {'PASS' if ok else '*** EDGE DIES ***'}",
              flush=True)

    btc = load_symbol(PRIMARY)
    n = len(btc)
    cut = int(n * (1 - HOLDOUT_FRAC))
    cut_ts = btc.index[cut]
    cov = btc["imb1"].notna().mean()
    base = Params()

    say("# OBI + stretch mean reversion: validation report")
    say()
    say(f"Generated {pd.Timestamp.now('UTC'):%Y-%m-%d %H:%M} UTC by `obi_meanrev.py`.")
    say()
    say(f"* Data: Binance USD-M perpetuals, {INTERVAL} bars, {btc.index[0]} to "
        f"{btc.index[-1]} ({n} BTC bars). OBI coverage: {cov:.1%} of bars.")
    say(f"* Costs: {FEE_BPS} bp fee + {SLIP_BPS} bp slippage per side "
        f"({2 * (FEE_BPS + SLIP_BPS):.0f} bp round trip, scaled by leverage).")
    say(f"* Sizing: vol target {TARGET_ANN_VOL:.0%} annualised, max leverage "
        f"{MAX_LEVERAGE}x. Fill at next bar open.")
    say(f"* In-sample: bars {WARMUP} to {cut} (to {cut_ts}). Holdout (30%): "
        f"{cut_ts} to {btc.index[-1]}.")
    say(f"* Default params: `{json.dumps(asdict(base))}`")
    say()

    # ---- 0. Plain backtest with defaults (full period, for reference) -------
    say("## 0. Reference backtest: BTC, default params, full period")
    say()
    rows = []
    for label, (a, b) in {"In-sample": (WARMUP, cut), "Holdout": (cut, n),
                          "Full": (WARMUP, n)}.items():
        res = run(btc, base, a, b, PRIMARY)
        m = metrics(res)
        m["segment"] = label
        m["bh"] = buy_hold(indicators(btc, base, PRIMARY), a, b)
        rows.append(m)
        g = metrics(run(btc, base, a, b, PRIMARY, cost=0.0))
        rows.append({**g, "segment": f"{label} (zero cost)", "bh": m["bh"]})
    say(md_table(rows, [("segment", "Segment", "s"), *METRIC_COLS, ("bh", "Buy&hold", "%")]))
    say()
    say("Zero-cost rows are diagnostics only: they show whether any gross edge "
        "exists before fees.")
    say()

    # ---- 1. Walk-forward ----------------------------------------------------
    say("## 1. Walk-forward (expanding window, inside the first 70%)")
    say()
    edges = np.linspace(WARMUP, cut, WF_FOLDS + 2).astype(int)
    wf_rows, oos_rets = [], []
    for f in range(1, WF_FOLDS + 1):
        tr0, tr1, te0, te1 = edges[0], edges[f], edges[f], edges[f + 1]
        p, mtr = optimise(btc, PRIMARY, tr0, tr1, base)
        res = run(btc, p, te0, te1, PRIMARY)
        mte = metrics(res)
        oos_rets.append(res["rets"])
        wf_rows.append({"fold": str(f), "test": f"{btc.index[te0]:%Y-%m-%d} to {btc.index[te1 - 1]:%Y-%m-%d}",
                        "params": f"anchor={p.anchor_len} entry={p.entry_z} obi={p.obi_min}",
                        "train_sharpe": mtr["sharpe"], **mte})
    say(md_table(wf_rows, [("fold", "Fold", "s"), ("test", "Test window", "s"),
                           ("params", "Chosen on train", "s"),
                           ("train_sharpe", "Train Sharpe", "f"), *METRIC_COLS[:5]]))
    stitched = {"rets": np.concatenate(oos_rets),
                "trades": []}
    sm = metrics(stitched)
    sm["trades"] = sum(r["trades"] for r in wf_rows)
    pos_frac = np.mean([r["return"] > 0 for r in wf_rows])
    say()
    say(f"Stitched OOS: return {sm['return']:.2%}, Sharpe {sm['sharpe']:.2f}, "
        f"max DD {sm['max_dd']:.2%}, trades {sm['trades']}, positive folds "
        f"{pos_frac:.0%}. Mean train Sharpe "
        f"{np.mean([r['train_sharpe'] for r in wf_rows]):.2f} vs mean test Sharpe "
        f"{np.mean([r['sharpe'] for r in wf_rows]):.2f}.")
    say()
    verdict("Walk-forward stitched OOS Sharpe", f"{sm['sharpe']:.2f}",
            f"> {PASS['wf_oos_sharpe_min']}", sm["sharpe"] > PASS["wf_oos_sharpe_min"])
    verdict("Walk-forward positive folds", f"{pos_frac:.0%}",
            f">= {PASS['wf_fold_pos_frac']:.0%}", pos_frac >= PASS["wf_fold_pos_frac"])

    # ---- 2. 30% holdout ------------------------------------------------------
    say("## 2. 30% out-of-sample holdout")
    say()
    p_final, m_is = optimise(btc, PRIMARY, WARMUP, cut, base)
    res_ho = run(btc, p_final, cut, n, PRIMARY)
    m_ho = metrics(res_ho)
    say(f"Params fit on the full in-sample (best IS Sharpe on the 27-point grid): "
        f"`{json.dumps(asdict(p_final))}`")
    say()
    say(md_table([{"segment": "In-sample (fit)", **m_is,
                   "bh": buy_hold(indicators(btc, p_final, PRIMARY), WARMUP, cut)},
                  {"segment": "Holdout", **m_ho,
                   "bh": buy_hold(indicators(btc, p_final, PRIMARY), cut, n)}],
                 [("segment", "Segment", "s"), *METRIC_COLS, ("bh", "Buy&hold", "%")]))
    say()
    verdict("Holdout Sharpe", f"{m_ho['sharpe']:.2f}",
            f"> {PASS['holdout_sharpe_min']}", m_ho["sharpe"] > PASS["holdout_sharpe_min"])
    verdict("Holdout return", f"{m_ho['return']:.2%}", "> 0", m_ho["return"] > 0)

    # ---- 3. Parameter sweep +/-25% ------------------------------------------
    say("## 3. Parameter sweep (every parameter at -25% / 0 / +25%, full grid)")
    say()
    if do_sweep:
        names = list(asdict(p_final).keys())
        grids = [sweep_values(k, getattr(p_final, k)) for k in names]
        combos = [dict(zip(names, v)) for v in itertools.product(*grids)]
        # warm indicator cache before forking so workers share it
        for a, z, ob in itertools.product(grids[0], grids[1], grids[2]):
            indicators(btc, replace(p_final, anchor_len=a, z_len=z, obi_len=ob), PRIMARY)
        _SWEEP_CTX.update(df=btc, key=PRIMARY, segs={"is": (WARMUP, cut), "ho": (cut, n)})
        t0 = time.time()
        with Pool(os.cpu_count() or 2) as pool:
            sw = pd.DataFrame(pool.map(_sweep_worker, combos, chunksize=32))
        sw.to_csv(os.path.join(RESULTS_DIR, "sweep.csv"), index=False)
        say(f"{len(sw)} combinations ({time.time() - t0:.0f}s). Zero-valued params "
            f"were swept +/-0.25 absolute. Full grid in `results/sweep.csv`.")
        say()
        srows = []
        for seg in ("is", "ho"):
            s = sw[f"{seg}_sharpe"]
            srows.append({"seg": "In-sample" if seg == "is" else "Holdout",
                          "pos": (s > 0).mean(), "med": s.median(),
                          "p10": s.quantile(0.1), "p90": s.quantile(0.9),
                          "rpos": (sw[f"{seg}_return"] > 0).mean()})
        say(md_table(srows, [("seg", "Segment", "s"), ("pos", "Share Sharpe>0", "%"),
                             ("rpos", "Share return>0", "%"), ("med", "Median Sharpe", "f"),
                             ("p10", "10th pct", "f"), ("p90", "90th pct", "f")]))
        say()
        # one-at-a-time sensitivity on the holdout
        oat = []
        for k, g in zip(names, grids):
            for v in g:
                q = {**asdict(p_final), k: v}
                row = sw.loc[(sw[names] == pd.Series(q)).all(axis=1)].iloc[0]
                oat.append({"param": k, "value": str(v), "is": row["is_sharpe"],
                            "ho": row["ho_sharpe"], "tr": row["ho_trades"]})
        say("One-at-a-time (others at fitted values):")
        say()
        say(md_table(oat, [("param", "Param", "s"), ("value", "Value", "s"),
                           ("is", "IS Sharpe", "f"), ("ho", "Holdout Sharpe", "f"),
                           ("tr", "Holdout trades", "i")]))
        say()
        hp = (sw["ho_sharpe"] > 0).mean()
        verdict("Sweep: share of +/-25% grid with holdout Sharpe > 0", f"{hp:.0%}",
                f">= {PASS['sweep_pos_frac']:.0%}", hp >= PASS["sweep_pos_frac"])
    else:
        say("Skipped (--no-sweep).")
        say()

    # ---- 4. Cross-asset ------------------------------------------------------
    say("## 4. Cross-asset (BTC-fitted params frozen)")
    say()
    crows = []
    for sym in CROSS_ASSETS:
        d = load_symbol(sym)
        ci = int(d.index.searchsorted(cut_ts))
        for label, (a, b) in {"Full": (WARMUP, len(d)), "Holdout": (ci, len(d))}.items():
            m = metrics(run(d, p_final, a, b, sym))
            crows.append({"asset": sym, "segment": label, **m,
                          "bh": buy_hold(indicators(d, p_final, sym), a, b),
                          "cov": d["imb1"].notna().mean()})
    say(md_table(crows, [("asset", "Asset", "s"), ("segment", "Segment", "s"),
                         *METRIC_COLS[:6], ("bh", "Buy&hold", "%"), ("cov", "OBI cov", "%")]))
    say()
    for r in crows:
        if r["segment"] == "Full":
            verdict(f"Cross-asset {r['asset']} full-period Sharpe", f"{r['sharpe']:.2f}",
                    f"> {PASS['cross_sharpe_min']}", r["sharpe"] > PASS["cross_sharpe_min"])

    # ---- 5. Monte Carlo ------------------------------------------------------
    say(f"## 5. Monte Carlo: {MC_RUNS} bootstrap resamples of holdout trades")
    say()
    tr = np.array([t[4] for t in res_ho["trades"]])
    if len(tr) >= 2:
        years = (n - cut) / BARS_PER_YEAR
        mc = monte_carlo(tr, years)
        mc.to_csv(os.path.join(RESULTS_DIR, "monte_carlo.csv"), index=False)
        q = mc.quantile([0.05, 0.25, 0.5, 0.75, 0.95])
        mrows = [{"q": f"{int(i * 100)}th", **q.loc[i].to_dict()} for i in q.index]
        say(f"{len(tr)} holdout trades resampled with replacement, {MC_RUNS} runs.")
        say()
        say(md_table(mrows, [("q", "Percentile", "s"), ("return", "Return", "%"),
                             ("sharpe", "Sharpe (trade)", "f"), ("max_dd", "Max DD", "%"),
                             ("win_rate", "Win rate", "%")]))
        p_loss = (mc["return"] <= 0).mean()
        say()
        say(f"P(return <= 0) = {p_loss:.1%}; P(max DD worse than -20%) = "
            f"{(mc['max_dd'] < -0.20).mean():.1%}.")
        say()
        p5 = q.loc[0.05, "return"]
        verdict("Monte Carlo 5th-percentile return", f"{p5:.2%}",
                f"> {PASS['mc_p5_return_min']:.0%}", p5 > PASS["mc_p5_return_min"])
    else:
        say("Fewer than 2 holdout trades: Monte Carlo impossible.")
        verdict("Monte Carlo", "n/a", ">= 2 trades", False)

    # ---- Verdict ---------------------------------------------------------------
    say("## Verdict")
    say()
    say(md_table(verdicts, [("test", "Test", "s"), ("value", "Value", "s"),
                            ("rule", "Pass rule", "s"), ("result", "Result", "s")]))
    say()
    dead = [v["test"] for v in verdicts if v["result"] != "PASS"]
    if dead:
        say(f"**The edge dies in {len(dead)} of {len(verdicts)} tests:** "
            + "; ".join(dead) + ".")
    else:
        say(f"**All {len(verdicts)} tests passed.**")
    with open(os.path.join(RESULTS_DIR, "REPORT.md"), "w") as fh:
        fh.write("\n".join(rep) + "\n")
    print(f"\nReport written to {os.path.join(RESULTS_DIR, 'REPORT.md')}")
    return 0


def single_backtest() -> int:
    df = load_symbol(PRIMARY)
    p = Params()
    res = run(df, p, WARMUP, len(df), PRIMARY)
    m = metrics(res)
    print(f"{PRIMARY} {INTERVAL} {df.index[WARMUP]} -> {df.index[-1]}  params={asdict(p)}")
    for k, v in m.items():
        print(f"  {k:>14}: {v:.4f}" if isinstance(v, float) else f"  {k:>14}: {v}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backtest-only", action="store_true")
    ap.add_argument("--no-sweep", action="store_true")
    args = ap.parse_args()
    sys.exit(single_backtest() if args.backtest_only else harness(not args.no_sweep))
