#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XAUUSD 15-minute mean-reversion research system (single file).

Thesis
------
When XAUUSD is stretched far from a moving fair-value anchor and the order
flow / quote book leans *against* the stretch (bids building after a selloff,
offers building after a rally), the move is more likely liquidity noise than
information and price should revert toward the anchor.

Requirements
------------
    Python >= 3.10 (tested on 3.13)
    pip install "numpy>=1.26" "pandas>=2.1" "matplotlib>=3.8"   # matplotlib optional

Commands
--------
    python xauusd_meanrev.py write-config  [--out config.json]   # sample config
    python xauusd_meanrev.py download --start 2021-01-01 --end 2025-10-31
          # Dukascopy tick archive -> 15m bars with real bid/ask, tick volume and
          # top-of-book size imbalance, for XAUUSD XAGUSD EURUSD GBPUSD
    python xauusd_meanrev.py backtest [--config config.json]   # one backtest
    python xauusd_meanrev.py run      [--config config.json]   # full harness
    python xauusd_meanrev.py selftest                          # synthetic checks

Input dataset schemas (config "data" section, one entry per symbol)
------------------------------------------------------------------
format "bars"  (canonical, CSV/CSV.GZ, UTC timestamps):
    ts, bid_open, bid_high, bid_low, bid_close,
        ask_open, ask_high, ask_low, ask_close, tick_volume
    optional: l1_imb        mean top-of-book size imbalance (bid-ask)/(bid+ask)
              tickdir_imb   (upticks-downticks)/(upticks+downticks) of the mid
              obi_L1..obi_Ln  cumulative multi-level depth imbalance (if you
                              recorded real depth, e.g. MT5 market-book logger)
    Bid-only bars are accepted (ask columns missing): then a "spread" column
    (price units) is required, or the instrument's fallback_spread is used and
    the report flags spread as ASSUMED.
format "ticks": ts, bid, ask[, bid_size, ask_size]  -> aggregated to 15m.
format "dukascopy_node_bars": "path" = bid m15 CSV, "ask_path" = ask m15 CSV from
    `npx dukascopy-node -t m15 -p bid|ask -f csv` (timestamp,open,high,low,close,volume).
format "dukascopy_node_ticks": CSV from `npx dukascopy-node -t tick -f csv`
    (timestamp,askPrice,bidPrice,askVolume,bidVolume), read in chunks.
format "mt5_bars":  MT5 "Export Bars" file (<DATE> <TIME> <OPEN> <HIGH> <LOW>
    <CLOSE> <TICKVOL> <VOL> <SPREAD>, bid prices, spread in points).
format "mt5_ticks": MT5 "Export Ticks" file (<DATE> <TIME> <BID> <ASK> ...).
For MT5 files set "server_tz": "UTC", a fixed offset like "+02:00", or "NY+7"
(the common New-York-close server clock, UTC+2 winter / UTC+3 summer, DST
switching on US dates).

Order-book honesty
------------------
Spot XAUUSD is OTC: there is no centralised order book and no public
historical multi-level depth. The OBI source actually used is printed in every
report:
    depth_Lk      REAL multi-level depth (only if you supply obi_L* columns)
    l1_size       REAL top-of-book quote-size imbalance, single venue
                  (Dukascopy ECN bid/ask volumes) -- not full depth
    tick_direction  PROXY (tick-rule order flow from quote changes)
    clv_volume      PROXY (close-location-value x tick volume, bars only)

Everything is decided on the close of bar t and filled at the open of bar t+1
at the executable side (ask to buy, bid to sell) plus slippage. Stops are
checked intrabar against the executable side with gap-through fills.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import itertools
import json
import lzma
import math
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, fields, replace
from multiprocessing import Pool

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SEED = 20240611
BAR = "15min"
BARS_PER_DAY_EST = 92          # ~23h trading day of 15m bars
TICK_CHUNK = 2_000_000        # rows per chunk when reading large tick files
TRADING_DAYS = 252


# =============================================================================
# 1. CONFIGURATION
# =============================================================================
@dataclass
class InstrumentSpec:
    """Broker contract spec. Defaults are typical ECN values: REPLACE them with
    your broker's specification sheet (contract size, swaps and commission
    differ between brokers)."""
    symbol: str
    contract_size: float          # units per 1.0 lot (XAUUSD: 100 oz)
    point: float                  # price increment
    min_lot: float = 0.01
    lot_step: float = 0.01
    max_lot: float = 50.0
    commission_per_lot_side: float = 3.5   # account ccy per lot per side
    slippage: float = 0.0         # price units per fill, adverse
    swap_long: float = 0.0        # account ccy per lot per night (negative = cost)
    swap_short: float = 0.0
    triple_swap_weekday: int = 2  # weekday of the trading day charged 3 nights (Wed)
    fallback_spread: float = 0.0  # only used for bid-only data without spread
    quote_to_account: float = 1.0  # constant FX conversion (quote ccy -> account)
    dukascopy_scale: int = 1000   # integer price scale in Dukascopy bi5 files
    asset_class: str = "spot_metal_cfd"


DEFAULT_INSTRUMENTS = {
    "XAUUSD": InstrumentSpec("XAUUSD", 100.0, 0.01, slippage=0.05, swap_long=-30.0,
                             swap_short=10.0, fallback_spread=0.25, dukascopy_scale=1000),
    "XAGUSD": InstrumentSpec("XAGUSD", 5000.0, 0.001, slippage=0.003, swap_long=-10.0,
                             swap_short=2.0, fallback_spread=0.025, dukascopy_scale=1000),
    "EURUSD": InstrumentSpec("EURUSD", 100000.0, 0.00001, slippage=0.00002,
                             swap_long=-7.0, swap_short=2.5, fallback_spread=0.00008,
                             dukascopy_scale=100000, asset_class="spot_fx"),
    "GBPUSD": InstrumentSpec("GBPUSD", 100000.0, 0.00001, slippage=0.00002,
                             swap_long=-5.0, swap_short=1.0, fallback_spread=0.00012,
                             dukascopy_scale=100000, asset_class="spot_fx"),
}


@dataclass(frozen=True)
class StrategyParams:
    # anchor & stretch
    anchor_type: str = "ema"          # ema | sma | rolling_vwap | vwma | session_vwap
    anchor_len: int = 64              # bars (16h)
    stretch_mode: str = "atr"         # atr | std | z | pct
    entry_z: float = 2.0              # stretch threshold (both sides, scaled below)
    long_mult: float = 1.0            # asymmetric threshold multipliers
    short_mult: float = 1.0
    side_mode: str = "both"           # both | long | short
    # order-book / order-flow imbalance
    obi_source: str = "auto"          # auto | depth_L1.. | l1_size | tick_direction | clv_volume | none
    obi_len: int = 96
    obi_min: float = 0.5              # long needs obi_z >= +obi_min, short <= -obi_min
    obi_improving: bool = False       # also require obi_z rising (long) / falling (short)
    # regime / liquidity filters
    adx_max: float = 30.0             # 0 disables
    vol_max: float = 0.90             # max ATR percentile (rolling ~20d); >=1 disables
    spread_mult: float = 3.0          # spread <= mult x rolling median spread
    mr_confirm: str = "none"          # none | reversal_bar | neg_autocorr
    sessions: tuple = ("asia", "london", "overlap", "ny")
    # exits
    exit_anchor: bool = True
    atr_stop_mult: float = 2.0        # also defines sizing risk distance
    use_stop: bool = True
    max_hold: int = 24                # bars (6h)
    trail_atr: float = 0.0            # 0 disables trailing stop
    inval_z: float = 0.0              # 0 disables: exit if obi_z flips beyond -inval_z
    mae_atr: float = 0.0              # 0 disables: close-based max adverse excursion
    session_exit: bool = False        # flatten at the end of allowed sessions
    weekend_flat: bool = True         # flatten before the Friday close


@dataclass
class RiskConfig:
    initial_equity: float = 100_000.0
    account_currency: str = "USD"
    risk_per_trade: float = 0.005     # fixed fractional risk to the ATR stop
    max_exposure: float = 5.0         # max notional / equity
    max_positions: int = 1            # engine enforces one position per instrument
    max_daily_loss: float = 0.02      # flatten + stop for the trading day
    max_drawdown: float = 0.20        # flatten + halt permanently
    max_consec_losses: int = 4
    cooldown_bars: int = 16           # pause after the consecutive-loss limit
    ruin_level: float = 0.50          # MC ruin = equity <= 50% of start


@dataclass
class HarnessConfig:
    primary: str = "XAUUSD"
    cross_assets: tuple = ("XAGUSD", "EURUSD", "GBPUSD")
    holdout_frac: float = 0.30
    wf_folds: int = 5
    warmup_bars: int = 2200
    min_trades_select: int = 30
    mc_runs: int = 1800
    mc_block: int = 5
    seed: int = SEED
    opt_grid: dict = field(default_factory=lambda: {
        "anchor_type": ["ema", "rolling_vwap", "session_vwap"],
        "entry_z": [1.5, 2.0, 2.5],
        "obi_min": [0.0, 0.5, 1.0],
        "side_mode": ["both", "long", "short"],
    })
    sweep_params: tuple = ("obi_min", "entry_z", "atr_stop_mult", "anchor_len",
                           "max_hold", "vol_max")
    stress_spread: float = 1.5
    stress_slip: float = 2.0
    stress_commission: float = 1.5
    # pre-registered pass rules (fixed before any result is seen)
    rules: dict = field(default_factory=lambda: {
        "is_pf": 1.10, "is_sharpe": 0.50,
        "wf_pf": 1.0, "wf_sharpe": 0.0, "wf_pos_folds": 0.60,
        "ho_pf": 1.0, "ho_sharpe": 0.0, "ho_maxdd": 0.20, "ho_min_trades": 30,
        "sweep_is_pf_share": 0.70, "sweep_ho_pf_share": 0.50,
        "cross_pf": 1.0, "cross_sharpe": 0.0,
        "mc_p_neg": 0.10, "mc_p_ddbreach": 0.05,
        "stress_pf": 1.0,
        "conc_best_month_share": 0.50,
    })


@dataclass
class Config:
    data: dict = field(default_factory=lambda: {
        s: {"path": os.path.join("data", "bars", f"{s}_15m.csv.gz"), "format": "bars",
            "server_tz": "UTC"} for s in ("XAUUSD", "XAGUSD", "EURUSD", "GBPUSD")})
    instruments: dict = field(default_factory=lambda: copy.deepcopy(DEFAULT_INSTRUMENTS))
    params: StrategyParams = field(default_factory=StrategyParams)
    risk: RiskConfig = field(default_factory=RiskConfig)
    harness: HarnessConfig = field(default_factory=HarnessConfig)
    results_dir: str = "results_xauusd"


def config_to_dict(cfg: Config) -> dict:
    d = {"data": cfg.data, "results_dir": cfg.results_dir,
         "instruments": {k: asdict(v) for k, v in cfg.instruments.items()},
         "params": asdict(cfg.params), "risk": asdict(cfg.risk),
         "harness": asdict(cfg.harness)}
    d["params"]["sessions"] = list(cfg.params.sessions)
    d["harness"]["cross_assets"] = list(cfg.harness.cross_assets)
    d["harness"]["sweep_params"] = list(cfg.harness.sweep_params)
    return d


def config_from_dict(d: dict) -> Config:
    cfg = Config()
    if "data" in d:
        cfg.data = d["data"]
    if "results_dir" in d:
        cfg.results_dir = d["results_dir"]
    for k, v in d.get("instruments", {}).items():
        base = asdict(cfg.instruments.get(k, InstrumentSpec(k, 1.0, 0.01)))
        base.update(v)
        cfg.instruments[k] = InstrumentSpec(**base)
    if "params" in d:
        p = dict(d["params"])
        if "sessions" in p:
            p["sessions"] = tuple(p["sessions"])
        cfg.params = replace(cfg.params, **p)
    if "risk" in d:
        cfg.risk = replace(cfg.risk, **d["risk"])
    if "harness" in d:
        h = dict(d["harness"])
        for t in ("cross_assets", "sweep_params"):
            if t in h:
                h[t] = tuple(h[t])
        cfg.harness = replace(cfg.harness, **h)
    return cfg


def load_config(path: str | None) -> Config:
    if not path:
        return Config()
    with open(path) as fh:
        return config_from_dict(json.load(fh))


# =============================================================================
# 2. DATA: DOWNLOAD, LOAD, AGGREGATE, VALIDATE
# =============================================================================
DUKA_URL = "https://datafeed.dukascopy.com/datafeed/{sym}/{y}/{m:02d}/{d:02d}/{h:02d}h_ticks.bi5"


def _duka_hour(sym: str, ts: pd.Timestamp, scale: int, retries: int = 5):
    """One Dukascopy hourly tick file -> DataFrame(ts, bid, ask, bid_size, ask_size).
    Record: >IIIff = ms offset, ask, bid, ask volume, bid volume. Month is 0-based."""
    url = DUKA_URL.format(sym=sym, y=ts.year, m=ts.month - 1, d=ts.day, h=ts.hour)
    for a in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                blob = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 ** a)
        except Exception:
            time.sleep(2 ** a)
    else:
        raise RuntimeError(f"Dukascopy fetch failed: {url}")
    if not blob:
        return None
    raw = lzma.decompress(blob)
    n = len(raw) // 20
    if n == 0:
        return None
    arr = np.frombuffer(raw[: n * 20], dtype=np.dtype([("ms", ">u4"), ("ask", ">u4"),
                                                        ("bid", ">u4"), ("av", ">f4"),
                                                        ("bv", ">f4")]))
    return pd.DataFrame({
        "ts": ts.tz_localize("UTC") + pd.to_timedelta(arr["ms"].astype(np.int64), unit="ms"),
        "bid": arr["bid"] / scale, "ask": arr["ask"] / scale,
        "bid_size": arr["bv"].astype(float), "ask_size": arr["av"].astype(float)})


def ticks_to_bars(t: pd.DataFrame) -> pd.DataFrame:
    """Aggregate quotes to 15m bars: bid/ask OHLC, tick count, top-of-book size
    imbalance (if sizes exist) and tick-rule direction imbalance (proxy)."""
    t = t.sort_values("ts")
    t = t[(t["bid"] > 0) & (t["ask"] >= t["bid"])]
    b = t["ts"].dt.floor(BAR)
    g = t.groupby(b)
    out = pd.DataFrame({
        "bid_open": g["bid"].first(), "bid_high": g["bid"].max(),
        "bid_low": g["bid"].min(), "bid_close": g["bid"].last(),
        "ask_open": g["ask"].first(), "ask_high": g["ask"].max(),
        "ask_low": g["ask"].min(), "ask_close": g["ask"].last(),
        "tick_volume": g.size().astype(float)})
    mid = (t["bid"] + t["ask"]) / 2
    d = np.sign(mid.diff().fillna(0.0))
    up = (d > 0).groupby(b).sum()
    dn = (d < 0).groupby(b).sum()
    out["tickdir_imb"] = ((up - dn) / (up + dn).replace(0, np.nan)).astype(float)
    if "bid_size" in t and "ask_size" in t:
        s = t["bid_size"] + t["ask_size"]
        imb = ((t["bid_size"] - t["ask_size"]) / s.replace(0, np.nan))
        out["l1_imb"] = imb.groupby(b).mean()
    out.index.name = "ts"
    return out


def download_dukascopy(symbols, start, end, cfg: Config, workers: int = 4) -> None:
    os.makedirs(os.path.join(HERE, "data", "bars"), exist_ok=True)
    hours = pd.date_range(start, pd.Timestamp(end) + pd.Timedelta(hours=23), freq="h")
    hours = hours[hours.dayofweek != 5]                 # Saturday has no quotes
    for sym in symbols:
        scale = cfg.instruments[sym].dukascopy_scale
        out_path = os.path.join(HERE, "data", "bars", f"{sym}_15m.csv.gz")
        print(f"[download] {sym}: {len(hours)} hourly tick files from Dukascopy ...",
              flush=True)
        parts, done = [], 0

        def job(h):
            df = _duka_hour(sym, h, scale)
            return None if df is None else ticks_to_bars(df)

        with ThreadPoolExecutor(workers) as ex:
            for res in ex.map(job, hours):
                done += 1
                if res is not None and len(res):
                    parts.append(res)
                if done % 2000 == 0:
                    print(f"  {sym}: {done}/{len(hours)}", flush=True)
        bars = pd.concat(parts).sort_index()
        bars = bars[~bars.index.duplicated(keep="last")]
        bars.to_csv(out_path, compression="gzip")
        print(f"[download] {sym}: {len(bars)} bars -> {out_path}")


def _server_to_utc(local: pd.Series, server_tz: str) -> pd.Series:
    if server_tz in ("UTC", "Z", "", None):
        return local.dt.tz_localize("UTC")
    if server_tz == "NY+7":            # NY-close broker clock, DST on US dates
        ny = (local - pd.Timedelta(hours=7)).dt.tz_localize(
            "America/New_York", ambiguous="NaT", nonexistent="shift_forward")
        return ny.dt.tz_convert("UTC")
    sign = -1 if server_tz.startswith("-") else 1
    hh, mm = server_tz.lstrip("+-").split(":")
    off = pd.Timedelta(hours=int(hh), minutes=int(mm)) * sign
    return (local - off).dt.tz_localize("UTC")


def load_bars(sym: str, cfg: Config) -> tuple[pd.DataFrame, dict]:
    """Load + clean one instrument into the canonical 15m bar frame."""
    spec = cfg.instruments[sym]
    src = cfg.data.get(sym)
    if not src:
        raise FileNotFoundError(f"no data entry for {sym} in config")
    path = src["path"] if os.path.isabs(src["path"]) else os.path.join(HERE, src["path"])
    if not os.path.exists(path):
        raise FileNotFoundError(f"{sym}: data file not found: {path}")
    fmt = src.get("format", "bars")
    tz = src.get("server_tz", "UTC")
    notes = []
    if fmt == "bars":
        df = pd.read_csv(path)
        df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.set_index("ts")
    elif fmt == "ticks":
        t = pd.read_csv(path)
        t["ts"] = pd.to_datetime(t["ts"], utc=True)
        df = ticks_to_bars(t)
    elif fmt == "dukascopy_node_bars":
        # npx dukascopy-node -i xauusd -t m15 -p bid|ask -f csv
        # columns: timestamp(ms UTC),open,high,low,close,volume
        ask_path = src["ask_path"] if os.path.isabs(src["ask_path"]) else os.path.join(HERE, src["ask_path"])
        parts = {}
        for side, pth in (("bid", path), ("ask", ask_path)):
            x = pd.read_csv(pth)
            x.index = pd.DatetimeIndex(pd.to_datetime(x["timestamp"], unit="ms", utc=True), name="ts")
            parts[side] = x
        b, a = parts["bid"], parts["ask"]
        df = pd.DataFrame({f"bid_{c}": b[c] for c in ("open", "high", "low", "close")})
        df = df.join(pd.DataFrame({f"ask_{c}": a[c] for c in ("open", "high", "low", "close")}), how="inner")
        df["tick_volume"] = b["volume"].reindex(df.index).astype(float)
        notes.append("dukascopy-node m15 candles: 'tick_volume' is Dukascopy traded-liquidity volume; "
                     "no quote sizes, so OBI falls back to a PROXY unless tick files are used")
    elif fmt == "dukascopy_node_ticks":
        # npx dukascopy-node -i xauusd -t tick -f csv
        # columns: timestamp(ms UTC),askPrice,bidPrice,askVolume,bidVolume (GB-sized: read in chunks)
        bars, carry = [], None
        for ch in pd.read_csv(path, chunksize=TICK_CHUNK):
            ch = ch.rename(columns={"askPrice": "ask", "bidPrice": "bid",
                                    "askVolume": "ask_size", "bidVolume": "bid_size"})
            ch["ts"] = pd.to_datetime(ch["timestamp"], unit="ms", utc=True)
            if carry is not None:
                ch = pd.concat([carry, ch], ignore_index=True)
            last_bucket = ch["ts"].iloc[-1].floor(BAR)
            keep = ch["ts"] >= last_bucket          # unfinished bucket waits for the next chunk
            carry = ch[keep]
            if (~keep).any():
                bars.append(ticks_to_bars(ch[~keep]))
        if carry is not None and len(carry):
            bars.append(ticks_to_bars(carry))
        df = pd.concat(bars)
        df = df[~df.index.duplicated(keep="last")]
    elif fmt == "mt5_bars":
        raw = pd.read_csv(path, sep=None, engine="python")
        raw.columns = [c.strip("<>").lower() for c in raw.columns]
        local = pd.to_datetime(raw["date"] + " " + raw["time"], format="%Y.%m.%d %H:%M:%S")
        ts = _server_to_utc(local, tz)
        sp = raw["spread"].astype(float) * spec.point
        df = pd.DataFrame({
            "bid_open": raw["open"].values, "bid_high": raw["high"].values,
            "bid_low": raw["low"].values, "bid_close": raw["close"].values,
            "spread": sp.values, "tick_volume": raw["tickvol"].astype(float).values},
            index=pd.DatetimeIndex(ts, name="ts"))
        notes.append("MT5 bars: ask reconstructed as bid + bar spread (MT5 stores the "
                     "minimum spread of the bar, so spread costs may be UNDERSTATED)")
    elif fmt == "mt5_ticks":
        raw = pd.read_csv(path, sep=None, engine="python")
        raw.columns = [c.strip("<>").lower() for c in raw.columns]
        local = pd.to_datetime(raw["date"] + " " + raw["time"], format="mixed")
        t = pd.DataFrame({"ts": _server_to_utc(local, tz).values,
                          "bid": raw["bid"].ffill().values, "ask": raw["ask"].ffill().values})
        df = ticks_to_bars(t.dropna())
    else:
        raise ValueError(f"unknown data format {fmt}")
    df, q = validate_bars(df, spec)
    q["notes"] = notes + q.get("notes", [])
    q["source"] = {"path": src["path"], "format": fmt, "server_tz": tz}
    return df, q


def validate_bars(df: pd.DataFrame, spec: InstrumentSpec) -> tuple[pd.DataFrame, dict]:
    """Structural cleaning. Only drops rows that are physically impossible; price
    spikes are flagged, not deleted."""
    q = {"rows_in": int(len(df)), "notes": []}
    df = df[~df.index.isna()]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df = df.sort_index()
    dup = df.index.duplicated(keep="last")
    q["duplicates_dropped"] = int(dup.sum())
    df = df[~dup].copy()
    # misaligned timestamps (not on a 15m boundary) -> re-bucket by floor
    off = (df.index != df.index.floor(BAR))
    q["misaligned_timestamps"] = int(off.sum())
    if off.any():
        df.index = df.index.floor(BAR)
        df = df[~df.index.duplicated(keep="last")]
    has_ask = all(c in df for c in ("ask_open", "ask_high", "ask_low", "ask_close"))
    if not has_ask:
        if "spread" in df and df["spread"].notna().any():
            sp = df["spread"].fillna(df["spread"].median()).clip(lower=spec.point)
            q["notes"].append("ask = bid + reported spread")
        else:
            sp = pd.Series(spec.fallback_spread, index=df.index)
            q["notes"].append(f"NO SPREAD IN DATA: constant ASSUMED spread "
                              f"{spec.fallback_spread} used")
        for c in ("open", "high", "low", "close"):
            df[f"ask_{c}"] = df[f"bid_{c}"] + sp
    q["spread_source"] = "data (bid/ask)" if has_ask else q["notes"][-1]
    if "tick_volume" not in df:
        df["tick_volume"] = 1.0
        q["notes"].append("no tick volume: VWAP anchors degrade to equal weights")
    cols = [f"{s}_{c}" for s in ("bid", "ask") for c in ("open", "high", "low", "close")]
    bad = df[cols].isna().any(axis=1) | (df[cols] <= 0).any(axis=1)
    for s in ("bid", "ask"):
        o, h, l, c = (df[f"{s}_{x}"] for x in ("open", "high", "low", "close"))
        bad |= (h < np.maximum(o, c) - 1e-12) | (l > np.minimum(o, c) + 1e-12) | (h < l)
    bad |= df["ask_close"] < df["bid_close"] - 1e-12
    bad |= df["ask_open"] < df["bid_open"] - 1e-12
    q["invalid_rows_dropped"] = int(bad.sum())
    df = df[~bad]
    mid = (df["bid_close"] + df["ask_close"]) / 2
    r = np.log(mid).diff()
    mad = r.abs().rolling(500, min_periods=50).median()
    spikes = (r.abs() > 25 * mad) & mad.notna()
    q["price_spikes_flagged_not_removed"] = int(spikes.sum())
    sp = df["ask_close"] - df["bid_close"]
    q["spread_median"] = float(sp.median())
    q["spread_p99"] = float(sp.quantile(0.99))
    dt = df.index.to_series().diff()
    gap = dt > pd.Timedelta(BAR)
    weekend = dt > pd.Timedelta(hours=36)
    q["intraweek_gaps"] = int((gap & ~weekend).sum())
    q["weekend_gaps"] = int(weekend.sum())
    df["post_gap"] = (dt > pd.Timedelta(hours=1)).fillna(True).values
    q["rows_out"] = int(len(df))
    q["start"], q["end"] = str(df.index[0]), str(df.index[-1])
    q["has_l1_size"] = bool("l1_imb" in df and df["l1_imb"].notna().mean() > 0.5)
    q["has_tickdir"] = bool("tickdir_imb" in df and df["tickdir_imb"].notna().mean() > 0.5)
    q["depth_levels"] = sorted(c for c in df.columns if c.startswith("obi_L"))
    return df, q


# =============================================================================
# 3. SESSIONS / TRADING CALENDAR (DST aware)
# =============================================================================
SESS_CODES = {"off": 0, "asia": 1, "london": 2, "overlap": 3, "ny": 4}


def session_frame(idx: pd.DatetimeIndex) -> pd.DataFrame:
    def label(ix):
        ldn = ix.tz_convert("Europe/London")
        ny = ix.tz_convert("America/New_York")
        tk = ix.tz_convert("Asia/Tokyo")
        lh = ldn.hour + ldn.minute / 60
        nh = ny.hour + ny.minute / 60
        th = tk.hour + tk.minute / 60
        is_l = (lh >= 8) & (lh < 16.5)
        is_n = (nh >= 8) & (nh < 17)
        is_a = (th >= 9) & (th < 16)
        lab = np.where(is_l & is_n, 3, np.where(is_l, 2, np.where(is_n, 4,
                       np.where(is_a, 1, 0))))
        return lab

    out = pd.DataFrame(index=idx)
    out["sess"] = label(idx)
    out["sess_next"] = label(idx + pd.Timedelta(BAR))   # exchange clock, known in advance
    ny = idx.tz_convert("America/New_York")
    nh = ny.hour + ny.minute / 60
    # FX trading day rolls at 17:00 New York
    out["fx_day"] = (ny + pd.Timedelta(hours=7)).normalize().tz_localize(None).values.astype("datetime64[D]").astype(np.int64)
    out["fx_wd"] = (ny + pd.Timedelta(hours=7)).dayofweek
    out["rollover"] = (nh >= 16.75) & (nh < 18.0)                  # wide-spread window
    out["friday_late"] = (ny.dayofweek == 4) & (nh >= 16.25)        # flatten before close
    out["friday_no_entry"] = (ny.dayofweek == 4) & (nh >= 15.0)
    return out


# =============================================================================
# 4. INDICATORS
# =============================================================================
def wilder(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(alpha=1.0 / n, adjust=False).mean()


def atr_adx(h, l, c, n=14):
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = wilder(tr, n)
    up, dn = h.diff(), -l.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=h.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=h.index)
    pdi = 100 * wilder(pdm, n) / atr
    mdi = 100 * wilder(mdm, n) / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return atr, wilder(dx.fillna(0), n)


def anchor_series(df: pd.DataFrame, kind: str, n: int, fx_day: pd.Series) -> pd.Series:
    c = df["mid_close"]
    v = df["tick_volume"].clip(lower=1.0)
    tp = (df["mid_high"] + df["mid_low"] + df["mid_close"]) / 3
    if kind == "ema":
        return c.ewm(span=n, adjust=False).mean()
    if kind == "sma":
        return c.rolling(n, min_periods=n // 2).mean()
    if kind == "rolling_vwap":
        return (tp * v).rolling(n, min_periods=n // 2).sum() / v.rolling(n, min_periods=n // 2).sum()
    if kind == "vwma":
        return (c * v).rolling(n, min_periods=n // 2).sum() / v.rolling(n, min_periods=n // 2).sum()
    if kind == "session_vwap":
        pv = (tp * v).groupby(fx_day.values).cumsum()
        vv = v.groupby(fx_day.values).cumsum()
        sv = pv / vv
        # first few bars of a session: blend toward EMA so the anchor is not the bar itself
        cnt = v.groupby(fx_day.values).cumcount()
        ema = c.ewm(span=n, adjust=False).mean()
        w = np.clip(cnt / 8.0, 0, 1)
        return w * sv + (1 - w) * ema
    raise ValueError(kind)


OBI_LABEL = {"l1_size": "REAL top-of-book quote-size imbalance (single venue, L1 only)",
             "tick_direction": "PROXY: tick-rule order-flow imbalance (not order book)",
             "clv_volume": "PROXY: close-location x tick volume (not order book)",
             "none": "OBI disabled"}


def resolve_obi_source(df: pd.DataFrame, want: str) -> str:
    if want != "auto":
        return want
    lv = sorted(c for c in df.columns if c.startswith("obi_L"))
    if lv:
        return "depth_" + lv[-1][4:]
    if "l1_imb" in df and df["l1_imb"].notna().mean() > 0.5:
        return "l1_size"
    if "tickdir_imb" in df and df["tickdir_imb"].notna().mean() > 0.5:
        return "tick_direction"
    return "clv_volume"


def obi_label(src: str) -> str:
    if src.startswith("depth_"):
        return f"REAL multi-level depth imbalance ({src[6:]}, user supplied)"
    return OBI_LABEL.get(src, src)


def obi_raw(df: pd.DataFrame, src: str) -> pd.Series:
    if src.startswith("depth_"):
        return df["obi_" + src[6:]]
    if src == "l1_size":
        return df["l1_imb"]
    if src == "tick_direction":
        return df["tickdir_imb"]
    if src == "clv_volume":
        h, l, c = df["mid_high"], df["mid_low"], df["mid_close"]
        clv = ((c - l) - (h - c)) / (h - l).replace(0, np.nan)
        v = df["tick_volume"]
        return (clv * v).rolling(4, min_periods=1).sum() / v.rolling(4, min_periods=1).sum()
    if src == "none":
        return pd.Series(0.0, index=df.index)
    raise ValueError(src)


class FeatureStore:
    """Causal features, cached per (anchor, stretch, obi, spread-stress) key.
    Every value at index t uses only data up to the close of bar t."""

    def __init__(self, df: pd.DataFrame, spec: InstrumentSpec, name: str):
        if "post_gap" not in df:                 # frame did not go through validate_bars
            df, _ = validate_bars(df, spec)
        self.df, self.spec, self.name = df, spec, name
        self.cache: dict = {}
        d = df
        for c in ("open", "high", "low", "close"):
            d[f"mid_{c}"] = (d[f"bid_{c}"] + d[f"ask_{c}"]) / 2
        self.sess = session_frame(d.index)
        atr, adx = atr_adx(d["mid_high"], d["mid_low"], d["mid_close"], 14)
        self.base = {
            "atr": atr, "adx": adx,
            "atr_pct": atr.rolling(2000, min_periods=500).rank(pct=True),
            "spread": d["ask_close"] - d["bid_close"],
            "spread_med": (d["ask_close"] - d["bid_close"]).rolling(500, min_periods=50).median(),
            "ret": np.log(d["mid_close"]).diff(),
        }
        r = self.base["ret"]
        self.base["autocorr"] = r.rolling(192, min_periods=96).corr(r.shift(1))

    def get(self, p: StrategyParams, spread_factor: float = 1.0) -> dict:
        src = resolve_obi_source(self.df, p.obi_source)
        key = (p.anchor_type, p.anchor_len, p.stretch_mode, src, p.obi_len,
               p.spread_mult, spread_factor)
        if key in self.cache:
            return self.cache[key]
        d, b, s = self.df, self.base, self.sess
        fx = pd.Series(s["fx_day"].values, index=d.index)
        anc = anchor_series(d, p.anchor_type, p.anchor_len, fx)
        dev = d["mid_close"] - anc
        if p.stretch_mode == "atr":
            st = dev / b["atr"]
        elif p.stretch_mode == "std":
            st = dev / d["mid_close"].rolling(p.anchor_len, min_periods=p.anchor_len // 2).std()
        elif p.stretch_mode == "z":
            st = (dev - dev.rolling(p.anchor_len * 2, min_periods=p.anchor_len).mean()) / \
                dev.rolling(p.anchor_len * 2, min_periods=p.anchor_len).std()
        elif p.stretch_mode == "pct":
            st = (dev / anc) / 0.001          # units of 10 bp: entry_z 2 => 0.20%
        else:
            raise ValueError(p.stretch_mode)
        raw = obi_raw(d, src)
        mu = raw.rolling(p.obi_len, min_periods=p.obi_len // 2).mean()
        sd = raw.rolling(p.obi_len, min_periods=p.obi_len // 2).std()
        oz = ((raw - mu) / sd) if src != "none" else pd.Series(0.0, index=d.index)
        half = (d["ask_open"] - d["bid_open"]) / 2 * spread_factor
        halfh = (d["ask_close"] - d["bid_close"]) / 2 * spread_factor
        mo = d["mid_open"]
        spread_ok = b["spread"] <= p.spread_mult * b["spread_med"]
        base_ok = (~d["post_gap"].values) & (~s["rollover"].values) & \
            (~s["friday_no_entry"].values) & spread_ok.fillna(False).values & \
            np.isfinite(st.values) & np.isfinite(b["atr"].values) & (b["atr"].values > 0)
        reversal = np.sign(d["mid_close"] - d["mid_open"]).values
        f = {
            # executable prices with spread stress applied symmetrically around mid
            "bo": (mo - half).values, "ao": (mo + half).values,
            "bl": (d["mid_low"] - halfh).values, "ah": (d["mid_high"] + halfh).values,
            "bh": (d["mid_high"] - halfh).values, "al": (d["mid_low"] + halfh).values,
            "bc": (d["mid_close"] - halfh).values, "ac": (d["mid_close"] + halfh).values,
            "mc": d["mid_close"].values, "mo": mo.values,
            "ml": d["mid_low"].values, "mh": d["mid_high"].values,
            "anchor": anc.values, "stretch": st.values, "oz": oz.values,
            "odelta": oz.diff().values, "atr": b["atr"].values, "adx": b["adx"].values,
            "atrp": b["atr_pct"].values, "acorr": b["autocorr"].values,
            "rev": reversal, "spread": (b["spread"] * spread_factor).values,
            "base_ok": base_ok, "sess": s["sess"].values, "sess_next": s["sess_next"].values,
            "fx_day": s["fx_day"].values, "fx_wd": s["fx_wd"].values,
            "friday_late": s["friday_late"].values,
        }
        f = {k: (v.tolist() if isinstance(v, np.ndarray) else v) for k, v in f.items()}
        f["ts"] = d.index
        f["obi_src"] = src
        self.cache[key] = f
        return f


# =============================================================================
# 5-9. SIGNALS, SIZING, COSTS, EXECUTION, BACKTEST
# =============================================================================
@dataclass
class CostModel:
    spread_factor: float = 1.0
    slip_factor: float = 1.0
    commission_factor: float = 1.0
    swaps: bool = True
    zero: bool = False               # diagnostic: no spread/slip/commission/swap


def position_size(equity, atr, stop_mult, spread, slip, price, spec: InstrumentSpec,
                  risk: RiskConfig) -> float:
    """Fixed-fractional lots to the ATR stop, floored to the broker lot step."""
    dist = stop_mult * atr + spread + 2 * slip
    if not (dist > 0) or equity <= 0:
        return 0.0
    per_lot = dist * spec.contract_size * spec.quote_to_account
    lots = risk.risk_per_trade * equity / per_lot
    lots = min(lots, risk.max_exposure * equity / (price * spec.contract_size * spec.quote_to_account),
               spec.max_lot)
    lots = math.floor(lots / spec.lot_step + 1e-9) * spec.lot_step
    lots = round(lots, 8)
    return lots if lots >= spec.min_lot - 1e-12 else 0.0


def backtest(f: dict, p: StrategyParams, spec: InstrumentSpec, risk: RiskConfig,
             i0: int, i1: int, cost: CostModel = CostModel()) -> dict:
    """Bar-by-bar event loop on [i0, i1). Signal at close k -> fill at open k+1."""
    bo, ao, bl, ah, bh, al, bc, ac = (f[k] for k in ("bo", "ao", "bl", "ah", "bh", "al", "bc", "ac"))
    mc, mo, anchor, st, oz, od = (f[k] for k in ("mc", "mo", "anchor", "stretch", "oz", "odelta"))
    atr, adx, atrp, acorr, rev = (f[k] for k in ("atr", "adx", "atrp", "acorr", "rev"))
    spread, base_ok, sess, sess_next = f["spread"], f["base_ok"], f["sess"], f["sess_next"]
    fx_day, fx_wd, fri = f["fx_day"], f["fx_wd"], f["friday_late"]
    cs, q2a = spec.contract_size, spec.quote_to_account
    zero = cost.zero
    slip = 0.0 if zero else spec.slippage * cost.slip_factor
    comm = 0.0 if zero else spec.commission_per_lot_side * cost.commission_factor
    use_swap = cost.swaps and not zero
    if zero:   # diagnostic: remove spread entirely, every fill at mid
        bo = ao = mo
        bl = al = f["ml"]
        bh = ah = f["mh"]
        bc = ac = mc
    allowed = {SESS_CODES[s] for s in p.sessions}
    elong = -p.entry_z * p.long_mult
    eshort = p.entry_z * p.short_mult
    allow_long = p.side_mode in ("both", "long")
    allow_short = p.side_mode in ("both", "short")
    use_adx = p.adx_max > 0
    use_vol = p.vol_max < 1.0
    omin = p.obi_min
    no_obi = f["obi_src"] == "none"

    n = i1 - i0
    eq_curve = [0.0] * n
    trades = []
    cash = risk.initial_equity
    peak = cash
    pos, lots = 0, 0.0
    entry_px = entry_mid = stop = best = atr_e = risk_amt = 0.0
    entry_k = 0
    swap_acc = comm_acc = 0.0
    pend = None           # ("entry", side, lots, atr) | ("exit", reason)
    halted = False
    day_block = False
    day_start = cash
    cool_until = -1
    consec = 0
    stats = {"skipped_min_lot": 0, "daily_loss_breaches": 0, "dd_halt": False,
             "consec_pauses": 0}
    eq_entry = cash

    def close_trade(k, px, mid_ref, reason):
        nonlocal cash, pos, lots, consec, cool_until, comm_acc, swap_acc
        c_exit = comm * lots
        price_pnl = (px - entry_px) * pos * lots * cs * q2a
        gross_mid = (mid_ref - entry_mid) * pos * lots * cs * q2a
        net = price_pnl - comm_acc - c_exit + swap_acc
        cash += price_pnl - c_exit          # entry commission & swaps already in cash
        trades.append({
            "side": "long" if pos > 0 else "short", "entry_k": entry_k, "exit_k": k,
            "entry_px": entry_px, "exit_px": px, "lots": lots, "bars": k - entry_k + 1,
            "gross_mid": gross_mid, "exec_cost": gross_mid - price_pnl,
            "commission": comm_acc + c_exit, "swap": swap_acc, "net": net,
            "R": net / risk_amt if risk_amt > 0 else 0.0, "ret": net / eq_entry,
            "reason": reason, "eq_entry": eq_entry, "sess": sess[entry_k - 1] if entry_k > 0 else 0,
            "adx": adx[entry_k - 1] if entry_k > 0 else float("nan"),
            "atrp": atrp[entry_k - 1] if entry_k > 0 else float("nan")})
        consec = consec + 1 if net <= 0 else 0
        if consec >= risk.max_consec_losses:
            cool_until = k + risk.cooldown_bars
            consec = 0
            stats["consec_pauses"] += 1
        pos, lots = 0, 0.0
        comm_acc = swap_acc = 0.0

    for k in range(i0, i1):
        if k > i0 and fx_day[k] != fx_day[k - 1]:
            # rollover passed between bars: swap on open position, new trading day
            if pos != 0 and use_swap:
                nights = 3 if fx_wd[k - 1] == spec.triple_swap_weekday else 1
                sw = (spec.swap_long if pos > 0 else spec.swap_short) * lots * nights
                cash += sw
                swap_acc += sw
            unreal = 0.0 if pos == 0 else (((bc[k - 1] if pos > 0 else ac[k - 1]) - entry_px) * pos * lots * cs * q2a)
            day_start = cash + unreal
            day_block = False
        exited_now = False
        # ---- fills at open[k] ----
        if pend is not None:
            if pend[0] == "exit" and pos != 0:
                px = (bo[k] - slip) if pos > 0 else (ao[k] + slip)
                close_trade(k, px, mo[k], pend[1])
                exited_now = True
            elif pend[0] == "entry" and pos == 0:
                side, lt, a_e = pend[1], pend[2], pend[3]
                pos, lots = side, lt
                entry_px = (ao[k] + slip) if side > 0 else (bo[k] - slip)
                entry_mid, entry_k, atr_e = mo[k], k, a_e
                stop = entry_px - side * p.atr_stop_mult * a_e
                best = entry_px
                eq_entry = cash
                risk_amt = risk.risk_per_trade * cash
                comm_acc = comm * lots
                cash -= comm_acc
            pend = None
        # ---- intrabar stop (executable side, gap-through) ----
        if pos != 0 and p.use_stop:
            if pos > 0 and bl[k] <= stop:
                px = min(stop, bo[k]) - slip
                close_trade(k, px, px + slip + (ao[k] - bo[k]) / 2, "stop")
                exited_now = True
            elif pos < 0 and ah[k] >= stop:
                px = max(stop, ao[k]) + slip
                close_trade(k, px, px - slip - (ao[k] - bo[k]) / 2, "stop")
                exited_now = True
        # ---- mark to market at close[k] ----
        if pos != 0:
            mark = bc[k] if pos > 0 else ac[k]
            eq = cash + (mark - entry_px) * pos * lots * cs * q2a
        else:
            eq = cash
        eq_curve[k - i0] = eq
        peak = max(peak, eq)
        last = k == i1 - 1
        if last:
            if pos != 0:
                px = bc[k] - slip if pos > 0 else ac[k] + slip
                close_trade(k, px, mc[k], "end_of_data")
                eq_curve[k - i0] = cash
            break
        # ---- risk engine ----
        if not halted and eq <= peak * (1 - risk.max_drawdown):
            halted = True
            stats["dd_halt"] = True
            if pos != 0:
                pend = ("exit", "dd_halt")
            continue
        if not day_block and eq <= day_start * (1 - risk.max_daily_loss):
            day_block = True
            stats["daily_loss_breaches"] += 1
            if pos != 0:
                pend = ("exit", "daily_loss")
                continue
        # ---- exits decided at close[k] ----
        if pos != 0:
            reason = None
            held = k - entry_k + 1
            if p.weekend_flat and fri[k]:
                reason = "weekend"
            elif p.inval_z > 0 and ((pos > 0 and oz[k] <= -p.inval_z) or (pos < 0 and oz[k] >= p.inval_z)):
                reason = "invalidation"
            elif p.mae_atr > 0 and (entry_px - (bc[k] if pos > 0 else ac[k])) * pos >= p.mae_atr * atr_e:
                reason = "mae"
            elif p.exit_anchor and ((pos > 0 and mc[k] >= anchor[k]) or (pos < 0 and mc[k] <= anchor[k])):
                reason = "anchor"
            elif held >= p.max_hold:
                reason = "max_hold"
            elif p.session_exit and sess_next[k] not in allowed:
                reason = "session_end"
            if reason:
                pend = ("exit", reason)
            elif p.trail_atr > 0:
                if pos > 0:
                    best = max(best, bh[k])
                    stop = max(stop, best - p.trail_atr * atr_e)
                else:
                    best = min(best, al[k])
                    stop = min(stop, best + p.trail_atr * atr_e)
            continue
        # ---- entries decided at close[k] ----
        if halted or day_block or exited_now or k < cool_until or not base_ok[k]:
            continue
        if sess[k] not in allowed or (p.session_exit and sess_next[k] not in allowed):
            continue
        if use_adx and not (adx[k] <= p.adx_max):
            continue
        if use_vol and not (atrp[k] <= p.vol_max):
            continue
        s, z = st[k], oz[k]
        side = 0
        if allow_long and s <= elong and (no_obi or z >= omin) and \
                (not p.obi_improving or od[k] > 0):
            side = 1
        elif allow_short and s >= eshort and (no_obi or z <= -omin) and \
                (not p.obi_improving or od[k] < 0):
            side = -1
        if side == 0:
            continue
        if p.mr_confirm == "reversal_bar" and rev[k] != side:
            continue
        if p.mr_confirm == "neg_autocorr" and not (acorr[k] < 0):
            continue
        px = ac[k] if side > 0 else bc[k]
        lt = position_size(eq, atr[k], p.atr_stop_mult, spread[k], slip, px, spec, risk)
        if lt <= 0:
            stats["skipped_min_lot"] += 1
            continue
        pend = ("entry", side, lt, atr[k])
    return {"equity": np.asarray(eq_curve), "trades": trades, "stats": stats,
            "ts": f["ts"][i0:i1], "i0": i0, "i1": i1}


# =============================================================================
# 10. PERFORMANCE REPORTING
# =============================================================================
def _safe(x):
    return float(x) if x is not None and not math.isnan(float(x)) else float("nan")


def metrics(res: dict, risk: RiskConfig) -> dict:
    eq = pd.Series(res["equity"], index=res["ts"])
    tr = pd.DataFrame(res["trades"])
    start = risk.initial_equity
    out = {"start": str(eq.index[0]) if len(eq) else "", "end": str(eq.index[-1]) if len(eq) else ""}
    if len(eq) == 0:
        return out
    total = eq.iloc[-1] / start - 1
    yrs = max((eq.index[-1] - eq.index[0]).total_seconds() / (365.25 * 86400), 1e-9)
    daily = eq.resample("1D").last().dropna()
    dr = daily.pct_change().dropna()
    dr = dr[dr.index.dayofweek < 5] if len(dr) else dr
    sd = dr.std()
    dn = dr[dr < 0].std()
    dd = eq / eq.cummax() - 1
    mdd = dd.min()
    mdd_cash = (eq.cummax() - eq).max()
    out.update({
        "total_return": total,
        "annual_return": (1 + total) ** (1 / yrs) - 1 if total > -1 else -1.0,
        "net_profit": eq.iloc[-1] - start,
        "sharpe": dr.mean() / sd * math.sqrt(TRADING_DAYS) if sd > 0 else 0.0,
        "sortino": dr.mean() / dn * math.sqrt(TRADING_DAYS) if dn and dn > 0 else 0.0,
        "max_dd": mdd,
        "recovery_factor": (eq.iloc[-1] - start) / mdd_cash if mdd_cash > 0 else float("nan"),
        "trades": int(len(tr)),
    })
    if len(tr) == 0:
        out.update({k: float("nan") for k in ("win_rate", "profit_factor", "avg_win", "avg_loss",
                                              "expectancy", "expectancy_R", "avg_hold_bars")})
        out.update({"gross_profit_mid": 0.0, "total_costs": 0.0})
        return out
    w, l = tr[tr.net > 0], tr[tr.net <= 0]
    out.update({
        "win_rate": len(w) / len(tr),
        "profit_factor": w.net.sum() / -l.net.sum() if l.net.sum() < 0 else float("inf"),
        "avg_win": w.net.mean() if len(w) else 0.0,
        "avg_loss": l.net.mean() if len(l) else 0.0,
        "expectancy": tr.net.mean(), "expectancy_R": tr.R.mean(),
        "avg_hold_bars": tr.bars.mean(), "avg_hold_hours": tr.bars.mean() * 0.25,
        "gross_profit_mid": tr.gross_mid.sum(),
        "cost_exec_spread_slip": tr.exec_cost.sum(), "cost_commission": tr.commission.sum(),
        "swap_net": tr.swap.sum(),
        "total_costs": tr.exec_cost.sum() + tr.commission.sum() - tr.swap.sum(),
    })
    for side in ("long", "short"):
        s = tr[tr.side == side]
        sl = s[s.net <= 0].net.sum()
        out[f"{side}_trades"] = int(len(s))
        out[f"{side}_net"] = float(s.net.sum())
        out[f"{side}_win_rate"] = float((s.net > 0).mean()) if len(s) else float("nan")
        out[f"{side}_pf"] = float(s[s.net > 0].net.sum() / -sl) if sl < 0 else float("nan")
    out["exit_reasons"] = tr.reason.value_counts().to_dict()
    out.update({k: v for k, v in res["stats"].items()})
    return out


def monthly_returns(res: dict) -> pd.Series:
    eq = pd.Series(res["equity"], index=res["ts"])
    m = eq.resample("ME").last()
    prev = m.shift(1)
    prev.iloc[0] = eq.iloc[0]
    return (m / prev - 1).rename("return")


def trades_frame(res: dict) -> pd.DataFrame:
    tr = pd.DataFrame(res["trades"])
    if len(tr):
        ts = res["ts"]
        tr["entry_ts"] = [ts[i - res["i0"]] for i in tr.entry_k]
        tr["exit_ts"] = [ts[i - res["i0"]] for i in tr.exit_k]
        inv = {v: k for k, v in SESS_CODES.items()}
        tr["session"] = tr.sess.map(inv)
    return tr


def fmt(x, kind="f"):
    if isinstance(x, str):
        return x
    if x is None or (isinstance(x, float) and (math.isnan(x))):
        return "n/a"
    if isinstance(x, float) and math.isinf(x):
        return "inf"
    if kind == "%":
        return f"{x * 100:.2f}%"
    if kind == "i":
        return f"{int(x)}"
    if kind == "$":
        return f"{x:,.0f}"
    return f"{x:.2f}"


def md_table(rows, cols):
    head = "| " + " | ".join(c[1] for c in cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    body = ["| " + " | ".join(fmt(r.get(c[0]), c[2]) for c in cols) + " |" for r in rows]
    return "\n".join([head, sep, *body])


MCOLS = [("total_return", "Return", "%"), ("annual_return", "Ann.", "%"),
         ("trades", "Trades", "i"), ("win_rate", "Win", "%"), ("profit_factor", "PF", "f"),
         ("expectancy", "Exp $/tr", "$"), ("sharpe", "Sharpe", "f"),
         ("sortino", "Sortino", "f"), ("max_dd", "MaxDD", "%")]
MCOLS_FULL = MCOLS + [("avg_win", "Avg win", "$"), ("avg_loss", "Avg loss", "$"),
                      ("recovery_factor", "Recov", "f"), ("avg_hold_hours", "Hold h", "f"),
                      ("gross_profit_mid", "Gross $", "$"), ("total_costs", "Costs $", "$")]


def save_plots(res: dict, title: str, path_prefix: str) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return []
    eq = pd.Series(res["equity"], index=res["ts"])
    dd = eq / eq.cummax() - 1
    fig, ax = plt.subplots(2, 1, figsize=(11, 6), sharex=True,
                           gridspec_kw={"height_ratios": [3, 1]})
    ax[0].plot(eq.index, eq.values, lw=1)
    ax[0].set_title(f"{title}: equity")
    ax[0].grid(alpha=0.3)
    ax[1].fill_between(dd.index, dd.values * 100, 0, color="tab:red", alpha=0.5)
    ax[1].set_ylabel("DD %")
    ax[1].grid(alpha=0.3)
    fig.tight_layout()
    p1 = path_prefix + "_equity_drawdown.png"
    fig.savefig(p1, dpi=110)
    plt.close(fig)
    m = monthly_returns(res) * 100
    fig, ax = plt.subplots(1, 2, figsize=(11, 3.5))
    ax[0].bar(range(len(m)), m.values, color=["tab:green" if v > 0 else "tab:red" for v in m.values])
    ax[0].set_title(f"{title}: monthly returns %")
    ax[1].hist(m.values, bins=20, color="tab:blue", alpha=0.7)
    ax[1].set_title("monthly return distribution")
    fig.tight_layout()
    p2 = path_prefix + "_monthly.png"
    fig.savefig(p2, dpi=110)
    plt.close(fig)
    return [p1, p2]


# =============================================================================
# 11-15. VALIDATION HARNESS
# =============================================================================
_CTX: dict = {}


def _run_ctx(p: StrategyParams, i0: int, i1: int, cost: CostModel | None = None,
             fs_key: str = "primary") -> dict:
    fs, spec, risk = _CTX[fs_key]
    cost = cost or CostModel()
    return backtest(fs.get(p, cost.spread_factor), p, spec, risk, i0, i1, cost)


def _eval_worker(args):
    pdict, segs = args
    p = StrategyParams(**pdict)
    out = {}
    for name, (a, b) in segs.items():
        m = metrics(_run_ctx(p, a, b), _CTX["primary"][2])
        out[name] = {k: m.get(k) for k in ("total_return", "profit_factor", "sharpe",
                                            "trades", "max_dd")}
    return pdict, out


def _pmap(jobs, procs=None):
    procs = procs or max(1, (os.cpu_count() or 2))
    if procs == 1 or len(jobs) < 8:
        return [_eval_worker(j) for j in jobs]
    with Pool(procs) as pool:
        return pool.map(_eval_worker, jobs, chunksize=max(1, len(jobs) // (procs * 4)))


def _pdict(p: StrategyParams) -> dict:
    d = asdict(p)
    d["sessions"] = tuple(d["sessions"])
    return d


def optimise(base: StrategyParams, grid: dict, i0: int, i1: int, min_trades: int):
    combos = [replace(base, **dict(zip(grid, v))) for v in itertools.product(*grid.values())]
    _prewarm(combos)
    res = _pmap([(_pdict(c), {"t": (i0, i1)}) for c in combos])
    best, best_s = None, -1e18
    table = []
    for pdict, out in res:
        m = out["t"]
        table.append({**{k: pdict[k] for k in grid}, **m})
        if (m["trades"] or 0) >= min_trades and m["sharpe"] is not None and m["sharpe"] > best_s:
            best, best_s = StrategyParams(**pdict), m["sharpe"]
    return (best or base), pd.DataFrame(table), best is not None


def _prewarm(combos):
    fs = _CTX["primary"][0]
    for c in combos:
        fs.get(c)


def sweep_values(name, v):
    if name == "vol_max":
        return sorted({round(min(1.0, v * x), 4) for x in (0.75, 1.0, 1.25)})
    if v == 0:
        return [-0.25, 0.0, 0.25] if name != "obi_min" else [0.0, 0.25]
    vals = [v * 0.75, v, v * 1.25]
    if name in ("anchor_len", "max_hold"):
        vals = [max(2, int(round(x))) for x in vals]
    return vals


def mc_simulate(rets: np.ndarray, wins: np.ndarray, runs: int, block: int, seed: int,
                risk: RiskConfig, tpy: float) -> dict:
    """Trade-level Monte Carlo: iid bootstrap, random permutation and circular
    block bootstrap (dependence-aware). Each method runs `runs` simulations."""
    rng = np.random.default_rng(seed)
    n = len(rets)
    out = {}
    for method in ("iid_bootstrap", "permutation", "block_bootstrap"):
        rows = []
        for _ in range(runs):
            if method == "iid_bootstrap":
                idx = rng.integers(0, n, n)
            elif method == "permutation":
                idx = rng.permutation(n)
            else:
                nb = math.ceil(n / block)
                starts = rng.integers(0, n, nb)
                idx = ((starts[:, None] + np.arange(block)[None, :]) % n).ravel()[:n]
            s = rets[idx]
            eq = np.cumprod(1 + s)
            curve = np.concatenate([[1.0], eq])
            dd = (curve / np.maximum.accumulate(curve) - 1).min()
            lose = (s <= 0).astype(int)
            streak = cur = 0
            for x in lose:
                cur = cur + 1 if x else 0
                streak = max(streak, cur)
            sh = s.mean() / s.std() * math.sqrt(tpy) if s.std() > 0 else 0.0
            rows.append((eq[-1] - 1, dd, streak, curve.min() <= risk.ruin_level, sh))
        df = pd.DataFrame(rows, columns=["ret", "max_dd", "max_losing_streak", "ruin", "sharpe"])
        out[method] = df
    return out


def mc_daily(daily_ret: np.ndarray, runs: int, block: int, seed: int, risk: RiskConfig) -> dict:
    """Day-level circular block bootstrap for daily-loss / drawdown breach odds."""
    rng = np.random.default_rng(seed + 1)
    n = len(daily_ret)
    if n < 5:
        return {}
    br_day = br_dd = 0
    for _ in range(runs):
        nb = math.ceil(n / block)
        starts = rng.integers(0, n, nb)
        idx = ((starts[:, None] + np.arange(block)[None, :]) % n).ravel()[:n]
        s = daily_ret[idx]
        curve = np.concatenate([[1.0], np.cumprod(1 + s)])
        br_day += (s <= -risk.max_daily_loss).any()
        br_dd += ((curve / np.maximum.accumulate(curve) - 1).min() <= -risk.max_drawdown)
    return {"p_daily_loss_breach": br_day / runs, "p_drawdown_breach": br_dd / runs}


def lookahead_check(fs: FeatureStore, p: StrategyParams, spec, risk, cut: int) -> dict:
    """Replace everything after `cut` with noise: results before `cut` must not move."""
    df2 = fs.df.copy()
    rng = np.random.default_rng(1)
    n2 = len(df2) - cut
    mult = np.exp(np.cumsum(rng.normal(0, 0.01, n2)))
    for c in [c for c in df2.columns if c.startswith(("bid_", "ask_", "mid_"))]:
        df2.iloc[cut:, df2.columns.get_loc(c)] = df2[c].values[cut:] * mult
    for c in ("l1_imb", "tickdir_imb"):
        if c in df2:
            df2.iloc[cut:, df2.columns.get_loc(c)] = rng.uniform(-1, 1, n2)
    df2["tick_volume"] = df2["tick_volume"].astype(float)
    df2.iloc[cut:, df2.columns.get_loc("tick_volume")] = rng.uniform(1, 500, n2)
    fs2 = FeatureStore(df2[[c for c in df2.columns if not c.startswith("mid_")]].copy(), spec, "la")
    a = backtest(fs.get(p), p, spec, risk, 0, len(df2))
    b = backtest(fs2.get(p), p, spec, risk, 0, len(df2))
    ta = [(t["entry_k"], t["exit_k"], t["side"], round(t["net"], 6)) for t in a["trades"] if t["exit_k"] < cut - 1]
    tb = [(t["entry_k"], t["exit_k"], t["side"], round(t["net"], 6)) for t in b["trades"] if t["exit_k"] < cut - 1]
    same_eq = np.allclose(a["equity"][: cut - 2], b["equity"][: cut - 2])
    return {"trades_before_cut": len(ta), "identical_trades": ta == tb, "identical_equity": bool(same_eq)}


def data_fingerprint(df: pd.DataFrame) -> str:
    h = hashlib.sha256()
    h.update(f"{len(df)}|{df.index[0]}|{df.index[-1]}".encode())
    h.update(np.ascontiguousarray(df["bid_close"].values[:: max(1, len(df) // 5000)]).tobytes())
    return h.hexdigest()[:16]


def params_fingerprint(p: StrategyParams) -> str:
    return hashlib.sha256(json.dumps(_pdict(p), sort_keys=True, default=list).encode()).hexdigest()[:16]


def holdout_guard(results_dir: str, data_fp: str, params_fp: str) -> dict:
    """Records every holdout evaluation. If the frozen params change after the
    holdout has been seen, the holdout is reported as CONTAMINATED."""
    path = os.path.join(results_dir, "holdout_lock.json")
    lock = {}
    if os.path.exists(path):
        with open(path) as fh:
            lock = json.load(fh)
    entry = lock.get(data_fp)
    status = "first independent evaluation"
    if entry:
        if entry["params_fp"] != params_fp:
            status = (f"CONTAMINATED: holdout already evaluated on {entry['first_utc']} with "
                      f"different frozen params ({entry['params_fp']}); this run is not independent")
        else:
            status = f"re-run with identical frozen params (first seen {entry['first_utc']})"
        entry["runs"] = entry.get("runs", 1) + 1
    else:
        entry = {"params_fp": params_fp, "first_utc": pd.Timestamp.now("UTC").isoformat(), "runs": 1}
    lock[data_fp] = entry
    with open(path, "w") as fh:
        json.dump(lock, fh, indent=2)
    return {"status": status, "contaminated": status.startswith("CONTAMINATED")}


class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.verdicts: list[dict] = []
        self.summary: dict = {}

    def say(self, s: str = ""):
        print(s, flush=True)
        self.lines.append(s)

    def verdict(self, test, result, ok, why):
        self.verdicts.append({"test": test, "result": result,
                              "pass": "PASS" if ok else "FAIL (EDGE DIES)", "why": why})
        print(f"  >> {test}: {result} -> {'PASS' if ok else '*** FAIL: EDGE DIES ***'} ({why})",
              flush=True)


def run_harness(cfg: Config, quick: bool = False, label: str = "") -> dict:
    H, R, P0 = cfg.harness, cfg.risk, cfg.params
    rules = H.rules
    rdir = cfg.results_dir if os.path.isabs(cfg.results_dir) else os.path.join(HERE, cfg.results_dir)
    os.makedirs(rdir, exist_ok=True)
    rep = Report()
    say = rep.say

    # ---------------------------------------------------------------- data
    df, q = load_bars(H.primary, cfg)
    spec = cfg.instruments[H.primary]
    fs = FeatureStore(df, spec, H.primary)
    _CTX.clear()
    _CTX["primary"] = (fs, spec, R)
    n = len(df)
    w = min(H.warmup_bars, n // 10)
    cut = int(n * (1 - H.holdout_frac))
    cut_ts = df.index[cut]
    src = resolve_obi_source(df, P0.obi_source)
    with open(os.path.join(rdir, "data_quality.json"), "w") as fh:
        json.dump({H.primary: q}, fh, indent=2, default=str)

    say(f"# XAUUSD 15m mean-reversion: research & validation report {label}")
    say()
    say(f"Generated {pd.Timestamp.now('UTC'):%Y-%m-%d %H:%M} UTC by `xauusd_meanrev.py`. "
        f"Seed {H.seed}.")
    say()
    say("## Data and assumptions")
    say()
    say(f"* Primary: **{H.primary}** ({spec.asset_class}, broker spot/CFD feed, *not* COMEX "
        f"futures). {q['rows_out']} bars, {q['start']} to {q['end']}.")
    say(f"* Data quality: {q['duplicates_dropped']} duplicate and "
        f"{q['invalid_rows_dropped']} invalid rows dropped, {q['misaligned_timestamps']} "
        f"misaligned timestamps re-bucketed, {q['intraweek_gaps']} intraweek gaps (left as "
        f"gaps, no forward fill; first bar after a gap > 1h cannot trigger an entry), "
        f"{q['price_spikes_flagged_not_removed']} price spikes flagged but kept.")
    say(f"* Spread source: {q['spread_source']}. Median spread {q['spread_median']:.4f}, "
        f"99th pct {q['spread_p99']:.4f}.")
    for nt in q["notes"]:
        say(f"* NOTE: {nt}")
    say(f"* **Order-book input: `{src}` = {obi_label(src)}.** "
        + ("Genuine multi-level depth was NOT available; no historical order-book data is "
           "fabricated." if not src.startswith("depth_") else ""))
    say(f"* Contract: {spec.contract_size:g} units/lot, lots {spec.min_lot}-{spec.max_lot} step "
        f"{spec.lot_step}, commission {spec.commission_per_lot_side} {R.account_currency}/lot/side, "
        f"slippage {spec.slippage} per fill, swap long {spec.swap_long} / short {spec.swap_short} "
        f"per lot/night (x3 on weekday {spec.triple_swap_weekday}). Spot/CFD prices; swaps and "
        f"commissions are broker-specific placeholders unless set in config.")
    say(f"* Risk: {R.risk_per_trade:.2%} equity risked to the ATR stop, lots floored to the lot "
        f"step (trades below min lot are skipped), max exposure {R.max_exposure}x, daily loss stop "
        f"{R.max_daily_loss:.0%}, drawdown halt {R.max_drawdown:.0%}, {R.max_consec_losses} "
        f"consecutive losses -> {R.cooldown_bars}-bar pause, one position at a time.")
    say(f"* Execution: signal on bar close, fill next bar open at ask (buy) / bid (sell) + "
        f"slippage; intrabar stops on the executable side with gap-through fills; no entries in "
        f"the 16:45-18:00 NY rollover window or after 15:00 NY Friday; flat before Friday close.")
    say(f"* Split: warm-up {w} bars; in-sample bars {w}-{cut} (to {cut_ts}); "
        f"**holdout = final {H.holdout_frac:.0%}** ({cut_ts} to {df.index[-1]}).")
    say(f"* Baseline (a-priori) params: `{json.dumps(_pdict(P0), default=list)}`")
    say()

    la = lookahead_check(fs, P0, spec, R, cut=int(n * 0.5))
    say(f"Integrity: look-ahead test (all data after bar {int(n * 0.5)} replaced with noise): "
        f"{la['trades_before_cut']} earlier trades identical = {la['identical_trades']}, "
        f"equity identical = {la['identical_equity']}.")
    say()
    if not (la["identical_trades"] and la["identical_equity"]):
        say("**LOOK-AHEAD TEST FAILED: results below are not trustworthy.**")

    # ------------------------------------------------- research studies (IS only)
    say("## 1. Research studies (in-sample only, diagnostics, not used for selection)")
    say()
    studies = []

    def study(name, p):
        r = _run_ctx(p, w, cut)
        m = metrics(r, R)
        studies.append({"study": name, **m})
        return r, m

    for a in ("ema", "sma", "rolling_vwap", "vwma", "session_vwap"):
        study(f"anchor={a}", replace(P0, anchor_type=a))
    for sm in ("atr", "std", "z", "pct"):
        study(f"stretch={sm}", replace(P0, stretch_mode=sm))
    obis = ["none", "clv_volume"]
    if q["has_tickdir"]:
        obis.append("tick_direction")
    if q["has_l1_size"]:
        obis.append("l1_size")
    obis += ["depth_" + c[4:] for c in q["depth_levels"]]
    for o in obis:
        study(f"obi={o}", replace(P0, obi_source=o))
    exit_variants = {
        "exit: anchor (+max_hold cap)": dict(exit_anchor=True, use_stop=False),
        "exit: ATR stop (+cap)": dict(exit_anchor=False, use_stop=True),
        "exit: max_hold only": dict(exit_anchor=False, use_stop=False),
        "exit: trailing 1.5ATR (+cap)": dict(exit_anchor=False, use_stop=True, trail_atr=1.5),
        "exit: invalidation |z|>1 (+cap)": dict(exit_anchor=False, use_stop=False, inval_z=1.0),
        "exit: session end (+cap)": dict(exit_anchor=False, use_stop=False, session_exit=True),
        "exit: MAE 1.5ATR (+cap)": dict(exit_anchor=False, use_stop=False, mae_atr=1.5),
        "exit: baseline combo": {},
    }
    for nm, kw in exit_variants.items():
        study(nm, replace(P0, **kw))
    study("filters off (no ADX/vol filter)", replace(P0, adx_max=0, vol_max=1.0))
    study("mr_confirm=reversal_bar", replace(P0, mr_confirm="reversal_bar"))
    study("mr_confirm=neg_autocorr", replace(P0, mr_confirm="neg_autocorr"))
    study("long only", replace(P0, side_mode="long"))
    study("short only", replace(P0, side_mode="short"))
    say(md_table(studies, [("study", "Variant (IS)", "s"), *MCOLS]))
    say()
    # regime / session breakdown with filters off so every regime is represented
    r_all, _ = study("_regime_base", replace(P0, adx_max=0, vol_max=1.0))
    studies.pop()
    tr = trades_frame(r_all)
    reg_rows = []
    if len(tr):
        tr["adx_b"] = pd.cut(tr.adx, [-1, 20, 30, 1e9], labels=["ADX<20 (range)", "ADX 20-30", "ADX>30 (trend)"])
        tr["vol_b"] = pd.cut(tr.atrp, [-1, 1 / 3, 2 / 3, 2], labels=["low vol", "mid vol", "high vol"])
        for col in ("session", "adx_b", "vol_b", "side"):
            for g, s in tr.groupby(col, observed=True):
                sl = s[s.net <= 0].net.sum()
                reg_rows.append({"bucket": f"{col}: {g}", "trades": len(s),
                                 "win": (s.net > 0).mean(), "pf": s[s.net > 0].net.sum() / -sl if sl < 0 else float("nan"),
                                 "exp": s.net.mean(), "R": s.R.mean()})
    say("Regime / session breakdown (IS, filters off):")
    say()
    say(md_table(reg_rows, [("bucket", "Bucket", "s"), ("trades", "Trades", "i"), ("win", "Win", "%"),
                            ("pf", "PF", "f"), ("exp", "Exp $/tr", "$"), ("R", "Avg R", "f")]))
    say()
    pd.DataFrame(studies).drop(columns=["exit_reasons"], errors="ignore").to_csv(
        os.path.join(rdir, "studies_in_sample.csv"), index=False)

    # ------------------------------------------------------------ in-sample fit
    say("## 2. In-sample optimisation (grid on first 70% only)")
    say()
    grid = H.opt_grid if not quick else {k: v[:2] for k, v in H.opt_grid.items()}
    p_star, is_tab, found = optimise(P0, grid, w, cut, H.min_trades_select)
    is_tab.to_csv(os.path.join(rdir, "is_grid.csv"), index=False)
    r_is = _run_ctx(p_star, w, cut)
    m_is = metrics(r_is, R)
    say(f"Grid {json.dumps(grid)} ({len(is_tab)} combos). Selection: max IS Sharpe with >= "
        f"{H.min_trades_select} trades{'' if found else ' (no combo qualified: baseline kept)'}.")
    say(f"Frozen params: `{json.dumps(_pdict(p_star), default=list)}`")
    say()
    say(md_table([{"seg": "In-sample", **m_is}], [("seg", "Segment", "s"), *MCOLS_FULL]))
    say()
    is_ok = (m_is["trades"] >= H.min_trades_select and _safe(m_is.get("profit_factor")) > rules["is_pf"]
             and m_is["sharpe"] > rules["is_sharpe"])
    rep.verdict("In-sample", f"PF {fmt(m_is.get('profit_factor'))}, Sharpe {m_is['sharpe']:.2f}, "
                f"{m_is['trades']} trades", is_ok,
                f"needs PF > {rules['is_pf']} and Sharpe > {rules['is_sharpe']} (necessary, not sufficient)")

    # ------------------------------------------------------------ walk-forward
    say("## 3. Walk-forward (expanding train, next unseen block as test, inside first 70%)")
    say()
    edges = np.linspace(w, cut, H.wf_folds + 2).astype(int)
    folds, oos_parts, oos_trades = [], [], []
    for i in range(1, H.wf_folds + 1):
        a, b, c = edges[0], edges[i], edges[i + 1]
        pf_, _, _ = optimise(P0, grid, a, b, H.min_trades_select)
        mtr = metrics(_run_ctx(pf_, a, b), R)
        rte = _run_ctx(pf_, b, c)
        mte = metrics(rte, R)
        oos_parts.append(np.asarray(rte["equity"]) / R.initial_equity)
        oos_trades += rte["trades"]
        folds.append({"fold": str(i), "test": f"{df.index[b]:%Y-%m-%d} to {df.index[c - 1]:%Y-%m-%d}",
                      "chosen": f"{pf_.anchor_type}/z{pf_.entry_z}/obi{pf_.obi_min}/{pf_.side_mode}",
                      "train_sharpe": mtr["sharpe"], **mte})
    say(md_table(folds, [("fold", "Fold", "s"), ("test", "Test window", "s"), ("chosen", "Chosen on train", "s"),
                         ("train_sharpe", "Train Sharpe", "f"), *MCOLS]))
    # stitch: compound each fold's relative equity
    stitched, level = [], 1.0
    for part in oos_parts:
        stitched.append(part * level)
        level = stitched[-1][-1]
    st_eq = np.concatenate(stitched) * R.initial_equity
    st_ts = df.index[edges[1]:edges[-1]]
    m_wf = metrics({"equity": st_eq, "trades": oos_trades, "ts": st_ts, "stats": {}}, R)
    pos_folds = np.mean([f_["total_return"] > 0 for f_ in folds])
    eff = (np.mean([f_["sharpe"] for f_ in folds]) / np.mean([f_["train_sharpe"] for f_ in folds])
           if np.mean([f_["train_sharpe"] for f_ in folds]) > 0 else float("nan"))
    say()
    say(f"Aggregate OOS (stitched): return {m_wf['total_return']:.2%}, PF {fmt(m_wf.get('profit_factor'))}, "
        f"Sharpe {m_wf['sharpe']:.2f}, max DD {m_wf['max_dd']:.2%}, {m_wf['trades']} trades, "
        f"positive folds {pos_folds:.0%}, WF efficiency (test/train Sharpe) {fmt(eff)}.")
    say()
    pd.DataFrame(folds).to_csv(os.path.join(rdir, "walk_forward_folds.csv"), index=False)
    wf_ok = (_safe(m_wf.get("profit_factor")) > rules["wf_pf"] and m_wf["sharpe"] > rules["wf_sharpe"]
             and pos_folds >= rules["wf_pos_folds"])
    rep.verdict("Walk-forward", f"OOS PF {fmt(m_wf.get('profit_factor'))}, Sharpe {m_wf['sharpe']:.2f}, "
                f"{pos_folds:.0%} folds positive", wf_ok,
                f"needs PF > {rules['wf_pf']}, Sharpe > {rules['wf_sharpe']}, >= {rules['wf_pos_folds']:.0%} folds positive")

    # ------------------------------------------------------------ holdout
    say("## 4. Independent 30% holdout (frozen params, evaluated once)")
    say()
    guard = holdout_guard(rdir, data_fingerprint(df), params_fingerprint(p_star))
    r_ho = _run_ctx(p_star, cut, n)
    m_ho = metrics(r_ho, R)
    say(f"Holdout status: **{guard['status']}**.")
    say()
    say(md_table([{"seg": "Holdout", **m_ho}], [("seg", "Segment", "s"), *MCOLS_FULL]))
    say()
    say(f"Long: {m_ho.get('long_trades', 0)} trades, net {fmt(m_ho.get('long_net'), '$')}, PF "
        f"{fmt(m_ho.get('long_pf'))} | Short: {m_ho.get('short_trades', 0)} trades, net "
        f"{fmt(m_ho.get('short_net'), '$')}, PF {fmt(m_ho.get('short_pf'))}. Exit reasons: "
        f"{m_ho.get('exit_reasons', {})}. Daily-loss stops hit: {m_ho.get('daily_loss_breaches', 0)}, "
        f"drawdown halt: {m_ho.get('dd_halt', False)}.")
    say()
    ho_ok = (not guard["contaminated"] and m_ho["trades"] >= rules["ho_min_trades"]
             and m_ho["total_return"] > 0 and _safe(m_ho.get("profit_factor")) > rules["ho_pf"]
             and m_ho["sharpe"] > rules["ho_sharpe"] and m_ho["max_dd"] > -rules["ho_maxdd"])
    rep.verdict("30% holdout", f"return {m_ho['total_return']:.2%}, PF {fmt(m_ho.get('profit_factor'))}, "
                f"Sharpe {m_ho['sharpe']:.2f}, DD {m_ho['max_dd']:.2%}, {m_ho['trades']} trades", ho_ok,
                f"needs return > 0, PF > {rules['ho_pf']}, Sharpe > 0, DD < {rules['ho_maxdd']:.0%}, "
                f">= {rules['ho_min_trades']} trades, not contaminated")

    # ------------------------------------------------------------ cost analysis
    say("## 5. Gross vs net and transaction-cost stress (holdout)")
    say()
    zero = metrics(_run_ctx(p_star, cut, n, CostModel(zero=True)), R)
    stress_cost = CostModel(H.stress_spread, H.stress_slip, H.stress_commission)
    stress = metrics(_run_ctx(p_star, cut, n, stress_cost), R)
    say(md_table([{"seg": "Zero cost (mid fills)", **zero}, {"seg": "Base costs", **m_ho},
                  {"seg": f"Stress ({H.stress_spread}x spread, {H.stress_slip}x slip, "
                          f"{H.stress_commission}x comm)", **stress}],
                 [("seg", "Holdout", "s"), *MCOLS]))
    say()
    say(f"Base-case holdout costs: spread+slippage {fmt(m_ho.get('cost_exec_spread_slip'), '$')}, "
        f"commission {fmt(m_ho.get('cost_commission'), '$')}, net swap "
        f"{fmt(m_ho.get('swap_net'), '$')}; gross (mid-to-mid) P&L {fmt(m_ho.get('gross_profit_mid'), '$')}.")
    say()
    st_ok = _safe(stress.get("profit_factor")) > rules["stress_pf"] and stress["total_return"] > 0
    why = "needs holdout PF > 1 and return > 0 under stressed costs"
    if _safe(zero.get("profit_factor")) > 1 and _safe(m_ho.get("profit_factor")) <= 1:
        why += "; gross edge exists but costs eliminate it"
    rep.verdict("Cost stress", f"stressed PF {fmt(stress.get('profit_factor'))}, return "
                f"{stress['total_return']:.2%} (zero-cost PF {fmt(zero.get('profit_factor'))})", st_ok, why)

    # ------------------------------------------------------------ sweep
    say("## 6. Parameter sensitivity (+/-25%, full grid)")
    say()
    names = list(H.sweep_params)
    gvals = [sweep_values(k, getattr(p_star, k)) for k in names]
    if quick:
        combos = []
        for k, g in zip(names, gvals):
            for v in g:
                combos.append(replace(p_star, **{k: v}))
    else:
        combos = [replace(p_star, **dict(zip(names, v))) for v in itertools.product(*gvals)]
    _prewarm(combos)
    sres = _pmap([(_pdict(c), {"is": (w, cut), "ho": (cut, n)}) for c in combos])
    rows = []
    for pdict, out in sres:
        rows.append({**{k: pdict[k] for k in names},
                     **{f"{s}_{m}": out[s][m] for s in ("is", "ho") for m in out[s]}})
    sw = pd.DataFrame(rows).drop_duplicates(subset=names)
    sw.to_csv(os.path.join(rdir, "param_sweep.csv"), index=False)
    is_share = (sw.is_profit_factor > 1).mean()
    ho_share = (sw.ho_profit_factor > 1).mean()
    say(f"{len(sw)} combinations of {names} at -25%/0/+25% (vol_max capped at 1.0, a zero "
        f"obi_min swept to +0.25). Share with PF > 1: in-sample {is_share:.0%}, holdout "
        f"{ho_share:.0%}. Median Sharpe IS {sw.is_sharpe.median():.2f}, holdout "
        f"{sw.ho_sharpe.median():.2f}. Full table: `param_sweep.csv`.")
    say()
    oat = []
    for k, g in zip(names, gvals):
        for v in g:
            row = sw.loc[(sw[k] == v) & np.all([sw[o] == getattr(p_star, o) for o in names if o != k], axis=0)]
            if len(row):
                r0 = row.iloc[0]
                oat.append({"param": k, "value": str(v), "is_pf": r0.is_profit_factor, "is_sh": r0.is_sharpe,
                            "ho_pf": r0.ho_profit_factor, "ho_sh": r0.ho_sharpe, "ho_tr": r0.ho_trades})
    say(md_table(oat, [("param", "Param", "s"), ("value", "Value", "s"), ("is_pf", "IS PF", "f"),
                       ("is_sh", "IS Sharpe", "f"), ("ho_pf", "HO PF", "f"), ("ho_sh", "HO Sharpe", "f"),
                       ("ho_tr", "HO trades", "i")]))
    say()
    base_row = sw.loc[np.all([sw[o] == getattr(p_star, o) for o in names], axis=0)]
    knife = bool(len(base_row) and base_row.iloc[0].is_profit_factor > 1 and is_share < 0.5)
    worst = []
    for k in names:
        s = pd.DataFrame([o for o in oat if o["param"] == k])
        if len(s):
            worst.append((k, s.is_sh.max() - s.is_sh.min()))
    worst.sort(key=lambda x: -x[1])
    say(f"Most sensitive parameter (IS Sharpe range over its 3 values): "
        f"{', '.join(f'{k} ({v:.2f})' for k, v in worst[:3])}. Knife-edge optimum "
        f"(profitable at the fit but < 50% of neighbours profitable): {knife}.")
    say()
    sw_ok = is_share >= rules["sweep_is_pf_share"] and ho_share >= rules["sweep_ho_pf_share"] and not knife
    rep.verdict("Parameter sweep", f"PF>1 in {is_share:.0%} IS / {ho_share:.0%} holdout of grid", sw_ok,
                f"needs >= {rules['sweep_is_pf_share']:.0%} IS and >= {rules['sweep_ho_pf_share']:.0%} "
                f"holdout, no knife-edge")

    # ------------------------------------------------------------ concentration
    mr_all = pd.concat([monthly_returns(r_is), monthly_returns(r_ho)])
    pnl_month = mr_all.dropna().reset_index(drop=True)
    pos_sum = pnl_month[pnl_month > 0].sum()
    best_share = pnl_month.max() / pos_sum if pos_sum > 0 else float("nan")
    tot_ex_best = (1 + pnl_month.drop(index=pnl_month.idxmax())).prod() - 1 if len(pnl_month) > 1 else float("nan")
    say("## 7. Concentration (does it depend on one period?)")
    say()
    say(f"Months: {len(pnl_month)}, positive {(pnl_month > 0).mean():.0%}. Best month "
        f"{pnl_month.max():.2%} = {fmt(best_share, '%')} of all positive-month return. Compounded "
        f"IS+holdout return without the best month: {fmt(tot_ex_best, '%')}.")
    say()
    conc_ok = (np.isfinite(best_share) and best_share < rules["conc_best_month_share"]
               and np.isfinite(tot_ex_best) and tot_ex_best > 0)
    rep.verdict("Concentration", f"best month {fmt(best_share, '%')} of gains; ex-best return {fmt(tot_ex_best, '%')}",
                conc_ok, f"needs best month < {rules['conc_best_month_share']:.0%} of gains and positive ex-best")

    # ------------------------------------------------------------ cross-asset
    say("## 8. Cross-asset robustness (frozen XAUUSD params, instrument-specific contract & costs)")
    say()
    crow = []
    for sym in H.cross_assets:
        try:
            d2, q2 = load_bars(sym, cfg)
        except Exception as e:
            crow.append({"asset": sym, "seg": "MISSING DATA", "note": str(e)[:80]})
            rep.verdict(sym, "no data", False, f"dataset missing: cannot claim cross-asset validation ({e})")
            continue
        spec2 = cfg.instruments[sym]
        fs2 = FeatureStore(d2, spec2, sym)
        _CTX[sym] = (fs2, spec2, R)
        c2 = int(d2.index.searchsorted(cut_ts))
        w2 = min(H.warmup_bars, len(d2) // 10)
        src2 = resolve_obi_source(d2, p_star.obi_source)
        for seg, (a, b) in {"Full": (w2, len(d2)), "Holdout window": (max(c2, w2), len(d2))}.items():
            m2 = metrics(_run_ctx(p_star, a, b, fs_key=sym), R)
            crow.append({"asset": sym, "seg": seg, **m2, "note": f"obi={src2}"})
        full = crow[-2]
        ok = _safe(full.get("profit_factor")) > rules["cross_pf"] and full["sharpe"] > rules["cross_sharpe"]
        rep.verdict(sym, f"PF {fmt(full.get('profit_factor'))}, Sharpe {full['sharpe']:.2f}, "
                    f"{full['trades']} trades", ok,
                    f"needs full-period PF > {rules['cross_pf']} and Sharpe > {rules['cross_sharpe']}, params frozen")
    say(md_table(crow, [("asset", "Asset", "s"), ("seg", "Segment", "s"), *MCOLS, ("note", "Note", "s")]))
    say()

    # ------------------------------------------------------------ Monte Carlo
    say(f"## 9. Monte Carlo ({H.mc_runs} simulations per method, holdout trades)")
    say()
    tr_ho = pd.DataFrame(r_ho["trades"])
    mc_rows, mc_ok, mc_why = [], False, "fewer than 10 holdout trades"
    if len(tr_ho) >= 10:
        yrs = (df.index[-1] - cut_ts).total_seconds() / (365.25 * 86400)
        sims = mc_simulate(tr_ho.ret.values, tr_ho.net.values > 0, H.mc_runs, H.mc_block, H.seed, R,
                           len(tr_ho) / max(yrs, 1e-9))
        for mname, s in sims.items():
            s.to_csv(os.path.join(rdir, f"mc_{mname}.csv"), index=False)
            mc_rows.append({"method": mname, "med": s.ret.median(), "p5": s.ret.quantile(0.05),
                            "p95": s.ret.quantile(0.95), "dd_med": s.max_dd.median(),
                            "dd_p95": s.max_dd.quantile(0.05), "p_neg": (s.ret < 0).mean(),
                            "p_ddb": (s.max_dd <= -R.max_drawdown).mean(), "ruin": s.ruin.mean(),
                            "streak_med": s.max_losing_streak.median(),
                            "streak_p95": s.max_losing_streak.quantile(0.95)})
        daily = pd.Series(r_ho["equity"], index=r_ho["ts"]).resample("1D").last().dropna().pct_change().dropna()
        dmc = mc_daily(daily.values, H.mc_runs, H.mc_block, H.seed, R)
        say(md_table(mc_rows, [("method", "Method", "s"), ("med", "Median ret", "%"), ("p5", "5th pct", "%"),
                               ("p95", "95th pct", "%"), ("dd_med", "Median MaxDD", "%"),
                               ("dd_p95", "95th pct MaxDD", "%"), ("p_neg", "P(ret<0)", "%"),
                               ("p_ddb", f"P(DD>={R.max_drawdown:.0%})", "%"), ("ruin", "P(ruin)", "%"),
                               ("streak_med", "Med lose streak", "i"), ("streak_p95", "95% lose streak", "i")]))
        say()
        if dmc:
            say(f"Day-level block bootstrap ({H.mc_runs} runs): P(any day <= -{R.max_daily_loss:.0%}) = "
                f"{dmc['p_daily_loss_breach']:.1%}, P(drawdown >= {R.max_drawdown:.0%}) = "
                f"{dmc['p_drawdown_breach']:.1%}.")
            say()
        blk = [r for r in mc_rows if r["method"] == "block_bootstrap"][0]
        mc_ok = blk["p_neg"] <= rules["mc_p_neg"] and blk["p_ddb"] <= rules["mc_p_ddbreach"]
        mc_why = (f"block bootstrap needs P(ret<0) <= {rules['mc_p_neg']:.0%} and "
                  f"P(DD breach) <= {rules['mc_p_ddbreach']:.0%}")
        mc_res = f"P(ret<0) {blk['p_neg']:.1%}, 5th pct {blk['p5']:.2%}, P(DD breach) {blk['p_ddb']:.1%}"
    else:
        mc_res = f"{len(tr_ho)} trades"
    rep.verdict("Monte Carlo", mc_res, mc_ok, mc_why)

    # ------------------------------------------------------------ exports
    for nm, r in {"in_sample": r_is, "holdout": r_ho}.items():
        trades_frame(r).to_csv(os.path.join(rdir, f"trades_{nm}.csv"), index=False)
        eq = pd.Series(r["equity"], index=r["ts"], name="equity")
        pd.DataFrame({"equity": eq, "drawdown": eq / eq.cummax() - 1}).to_csv(
            os.path.join(rdir, f"equity_{nm}.csv"))
        monthly_returns(r).to_csv(os.path.join(rdir, f"monthly_{nm}.csv"))
        save_plots(r, f"{H.primary} {nm}", os.path.join(rdir, nm))
    pd.DataFrame({"equity": st_eq}, index=st_ts).to_csv(os.path.join(rdir, "equity_walk_forward_oos.csv"))

    # ------------------------------------------------------------ verdict
    say("## 10. Validation table")
    say()
    say(md_table(rep.verdicts, [("test", "Test", "s"), ("result", "Result", "s"), ("pass", "Pass/Fail", "s"),
                                ("why", "Explanation", "s")]))
    say()
    fails = [v["test"] for v in rep.verdicts if v["pass"] != "PASS"]
    core = {"Walk-forward", "30% holdout", "Cost stress"}
    cross_fail = [v for v in fails if v in H.cross_assets]
    if core & set(fails) or "In-sample" in fails:
        cls = "REJECTED"
        why = "no statistically credible, cost-adjusted edge: " + ", ".join(fails)
    elif len(fails) >= 2:
        cls = "RESEARCH PROMISING"
        why = "core OOS tests pass but robustness fails: " + ", ".join(fails)
    elif len(fails) == 1 and not cross_fail:
        cls = "RESEARCH PROMISING"
        why = "one robustness test fails: " + fails[0]
    elif m_ho["trades"] >= 100 and not fails and blk_p5_pos(mc_rows):
        cls = "LIVE-CANDIDATE"
        why = "all tests pass with >= 100 holdout trades and positive MC 5th percentile"
    else:
        cls = "PAPER-TRADING READY"
        why = "all core tests pass" + (f"; cross-asset caveat: {cross_fail}" if cross_fail else "")
    if fails:
        say(f"**The edge dies in {len(fails)} of {len(rep.verdicts)} tests: {', '.join(fails)}.**")
    say()
    say(f"## Final verdict: **{cls}**: {why}.")
    say()
    say(f"Order-book evidence: `{src}` = {obi_label(src)}.")
    rep.summary = {"verdict": cls, "reason": why, "failed_tests": fails, "validation": rep.verdicts,
                   "frozen_params": _pdict(p_star), "in_sample": m_is, "walk_forward_oos": m_wf,
                   "walk_forward_folds": folds, "holdout": m_ho, "holdout_status": guard,
                   "cost_zero": zero, "cost_stress": stress, "sweep_pf_share": {"is": is_share, "ho": ho_share},
                   "monte_carlo": mc_rows, "cross_asset": crow, "lookahead_check": la,
                   "obi_source": src, "obi_label": obi_label(src), "data_quality": q,
                   "config": config_to_dict(cfg)}
    with open(os.path.join(rdir, "REPORT.md"), "w") as fh:
        fh.write("\n".join(rep.lines) + "\n")
    with open(os.path.join(rdir, "summary.json"), "w") as fh:
        json.dump(rep.summary, fh, indent=2, default=lambda o: o if not isinstance(o, (np.generic,)) else o.item())
    print(f"\nReport: {os.path.join(rdir, 'REPORT.md')}")
    return rep.summary


def blk_p5_pos(mc_rows):
    b = [r for r in mc_rows if r["method"] == "block_bootstrap"]
    return bool(b and b[0]["p5"] > 0)


# =============================================================================
# 16. SELF-TEST WITH SYNTHETIC DATA (engine checks only; never a result)
# =============================================================================
def synth_bars(sym: str, spec: InstrumentSpec, start="2022-01-03", end="2025-06-30",
               edge: float = 0.0, seed: int = 0) -> pd.DataFrame:
    """Random-walk quotes with session volatility, weekend gaps, random spreads and
    random top-of-book sizes. With edge > 0 an OU reversion component is planted
    and the quote-size imbalance leans against it (a detectable mean-reversion edge)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, end, freq=BAR, tz="UTC")
    ny = idx.tz_convert("America/New_York")
    nh = ny.hour + ny.minute / 60
    closed = ((ny.dayofweek == 4) & (nh >= 17)) | (ny.dayofweek == 5) | ((ny.dayofweek == 6) & (nh < 17))
    idx = idx[~closed]
    n = len(idx)
    hr = idx.hour
    sess_vol = np.where((hr >= 12) & (hr < 17), 1.6, np.where((hr >= 7) & (hr < 12), 1.2, 0.7))
    base_px = {"XAUUSD": 2000.0, "XAGUSD": 25.0, "EURUSD": 1.10, "GBPUSD": 1.27}.get(sym, 100.0)
    sig = 0.0011 * sess_vol
    vol = np.exp(np.cumsum(rng.normal(0, 0.02, n)) * 0.1)
    rw = np.cumsum(rng.standard_t(5, n) * sig * vol / math.sqrt(5 / 3))
    ou = np.zeros(n)
    if edge > 0:
        kappa = 0.15
        e = rng.normal(0, 1, n) * 0.0018 * edge * sess_vol
        for i in range(1, n):
            ou[i] = ou[i - 1] * (1 - kappa) + e[i]
    lp = rw + ou
    close = base_px * np.exp(lp)
    op = np.concatenate([[close[0]], close[:-1]]) * np.exp(rng.normal(0, 0.00005, n))
    rngh = np.abs(rng.normal(0, 1, n)) * sig * close * 0.6
    high = np.maximum(op, close) + rngh
    low = np.minimum(op, close) - np.abs(rng.normal(0, 1, n)) * sig * close * 0.6
    half = spec.fallback_spread / 2 * np.exp(rng.normal(0, 0.25, n)) * np.where(sess_vol < 1, 1.4, 1.0)
    l1 = np.tanh(rng.normal(0, 0.6, n) - (ou / (0.004 * max(edge, 1e-9)) if edge > 0 else 0))
    df = pd.DataFrame({
        "bid_open": op - half, "bid_high": high - half, "bid_low": low - half, "bid_close": close - half,
        "ask_open": op + half, "ask_high": high + half, "ask_low": low + half, "ask_close": close + half,
        "tick_volume": np.round(rng.gamma(2.0, 150, n) * sess_vol),
        "l1_imb": l1, "tickdir_imb": np.tanh(rng.normal(0, 0.5, n))}, index=pd.DatetimeIndex(idx, name="ts"))
    return df


def selftest() -> int:
    tmp = os.path.join(HERE, "results_selftest")
    os.makedirs(os.path.join(tmp, "data"), exist_ok=True)
    ok_all = True
    for case, edge in (("no_edge", 0.0), ("planted_edge", 1.0)):
        cfg = Config()
        cfg.results_dir = os.path.join(tmp, case)
        if os.path.exists(os.path.join(cfg.results_dir, "holdout_lock.json")):
            os.remove(os.path.join(cfg.results_dir, "holdout_lock.json"))
        for i, sym in enumerate(("XAUUSD", "XAGUSD", "EURUSD", "GBPUSD")):
            p = os.path.join(tmp, "data", f"{case}_{sym}.csv.gz")
            synth_bars(sym, cfg.instruments[sym], edge=edge, seed=100 * i + int(edge * 7)).to_csv(p, compression="gzip")
            cfg.data[sym] = {"path": p, "format": "bars", "server_tz": "UTC"}
        cfg.harness = replace(cfg.harness, mc_runs=300)
        print(f"\n===== SELFTEST {case} (SYNTHETIC DATA: not a market result) =====")
        s = run_harness(cfg, quick=True, label=f"[SYNTHETIC SELFTEST: {case}]")
        la = s["lookahead_check"]
        ok = la["identical_trades"] and la["identical_equity"]
        if case == "no_edge":
            ok &= s["verdict"] == "REJECTED"
        print(f"SELFTEST {case}: verdict={s['verdict']} lookahead_ok={la['identical_trades'] and la['identical_equity']}"
              f" -> {'OK' if ok else 'UNEXPECTED'}")
        ok_all &= ok
    # unit checks: sizing never fractional, costs charged correctly
    spec = DEFAULT_INSTRUMENTS["XAUUSD"]
    lots = position_size(100_000, 3.0, 2.0, 0.3, 0.05, 2000, spec, RiskConfig())
    assert abs(lots / spec.lot_step - round(lots / spec.lot_step)) < 1e-9, "fractional lot step"
    assert position_size(100, 3.0, 2.0, 0.3, 0.05, 2000, spec, RiskConfig()) == 0.0, "min lot not enforced"
    print(f"unit: 100k equity, ATR 3, 2xATR stop -> {lots} lots (risk "
          f"{lots * (6.0 + 0.3 + 0.1) * 100:.0f} USD of 500 budget)")
    print("SELFTEST", "PASSED" if ok_all else "FAILED")
    return 0 if ok_all else 1


# =============================================================================
# CLI
# =============================================================================
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write-config")
    w.add_argument("--out", default="config.json")
    d = sub.add_parser("download")
    d.add_argument("--symbols", nargs="+", default=["XAUUSD", "XAGUSD", "EURUSD", "GBPUSD"])
    d.add_argument("--start", default="2021-01-01")
    d.add_argument("--end", default="2025-10-31")
    d.add_argument("--workers", type=int, default=4)
    d.add_argument("--config")
    b = sub.add_parser("backtest")
    b.add_argument("--config")
    r = sub.add_parser("run")
    r.add_argument("--config")
    r.add_argument("--quick", action="store_true", help="smaller grids (smoke test)")
    sub.add_parser("selftest")
    a = ap.parse_args(argv)
    if a.cmd == "write-config":
        with open(a.out, "w") as fh:
            json.dump(config_to_dict(Config()), fh, indent=2)
        print(f"wrote {a.out}")
        return 0
    if a.cmd == "download":
        download_dukascopy(a.symbols, a.start, a.end, load_config(a.config), a.workers)
        return 0
    if a.cmd == "selftest":
        return selftest()
    cfg = load_config(a.config)
    if a.cmd == "backtest":
        df, q = load_bars(cfg.harness.primary, cfg)
        spec = cfg.instruments[cfg.harness.primary]
        fs = FeatureStore(df, spec, "bt")
        res = backtest(fs.get(cfg.params), cfg.params, spec, cfg.risk,
                       min(cfg.harness.warmup_bars, len(df) // 10), len(df))
        m = metrics(res, cfg.risk)
        for k, v in m.items():
            print(f"{k:>24}: {fmt(v) if isinstance(v, float) else v}")
        return 0
    if a.cmd == "run":
        run_harness(cfg, quick=a.quick)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
