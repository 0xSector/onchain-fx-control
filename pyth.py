# @purpose pyth.py - Pyth intraday FX oracle fetcher (arb_sim_plan.html Slice-1 data source). Pulls a
#          1-minute fair-value time series for an FX corridor over a window from Pyth's free public
#          Benchmarks TradingView shim (no auth, no key), normalized to the tape's USD-per-local price
#          convention so it can be joined directly to the per-fill onchain tape (artifacts/tape/*.parquet).
#
#          Why Pyth here: the v1/m6b basis probe scores onchain fills against the ECB DAILY fix, which
#          cannot tell intraday drift (the fair value moving) apart from a capturable onchain dislocation
#          (the pool mid sitting off fair value). An intraday oracle separates the two. Pyth FX feeds are
#          24/7 1-min and free. (Any Pyth surface works; the Benchmarks shim is the simplest bulk-history
#          path - no Hermes-specific dependency.)
#
#          Convention: tape p = USD per local (EUR ~1.16, BRL ~0.197). Pyth FX.EUR/USD already quotes USD
#          per EUR (use as-is); Pyth FX.USD/BRL quotes BRL per USD (INVERT -> USD per BRL). Each corridor
#          declares its symbol + whether to invert. Cached to artifacts/pyth_fv_<pair>_<from>_<to>.csv.
#
#          Read-only network fetch of public market data. No auth, no wallet, no on-chain actions.

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

# pandas is imported LAZILY inside fetch_fv/fv_at (the bulk-history helpers) so the live path
# (live_lp -> pyth.fv_now, which uses only urllib/json) stays pandas-free and runs in the cloud cron
# with zero pip installs. Do NOT add a module-level `import pandas` back.

ART = Path(__file__).resolve().parent / "artifacts"
BENCH = "https://benchmarks.pyth.network/v1/shims/tradingview/history"
UA = {"User-Agent": "Mozilla/5.0 (onchain-fx-research; read-only)"}
DAY = 86_400

# corridor -> (Pyth TradingView symbol, invert?). invert=True turns BRL-per-USD into USD-per-BRL.
PYTH_SYMBOL = {
    "eur/usd": ("FX.EUR/USD", False),
    "brl/usd": ("FX.USD/BRL", True),
    "sgd/usd": ("FX.USD/SGD", True),
    "jpy/usd": ("FX.USD/JPY", True),
    "chf/usd": ("FX.USD/CHF", True),
    "gbp/usd": ("FX.GBP/USD", False),
}


def _get(symbol: str, frm: int, to: int, resolution: str = "1") -> dict:
    sym = symbol.replace("/", "%2F")
    url = f"{BENCH}?symbol={sym}&resolution={resolution}&from={frm}&to={to}"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def fetch_fv(pair: str, start_ts: int, end_ts: int, resolution: str = "1",
             reuse: bool = True) -> pd.DataFrame:
    """1-min fair-value series for `pair` over [start_ts, end_ts], USD-per-local. Day-chunked (the shim
    caps range per request). Columns: ts (bar open, unix s), fv (USD per local close). Cached."""
    import pandas as pd
    if pair not in PYTH_SYMBOL:
        raise ValueError(f"no Pyth symbol mapped for {pair}; add it to PYTH_SYMBOL")
    symbol, invert = PYTH_SYMBOL[pair]
    cache = ART / f"pyth_fv_{pair.replace('/', '_')}_{start_ts}_{end_ts}.csv"
    if reuse and cache.exists():
        return pd.read_csv(cache)

    rows: dict[int, float] = {}
    a = start_ts
    while a < end_ts:
        b = min(a + DAY, end_ts)
        for attempt in range(5):
            try:
                d = _get(symbol, a, b, resolution)
                break
            except urllib.error.HTTPError as e:  # 429 rate-limit -> longer backoff
                if attempt == 4:
                    raise
                time.sleep((4.0 if e.code == 429 else 1.5) * (attempt + 1))
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(1.5 * (attempt + 1))
        time.sleep(0.4)   # polite throttle between day-chunks
        if d.get("s") == "ok":
            for t, c in zip(d.get("t", []), d.get("c", [])):
                rows[int(t)] = (1.0 / float(c)) if invert else float(c)
        a = b
    out = (pd.DataFrame({"ts": list(rows), "fv": list(rows.values())})
           .sort_values("ts").reset_index(drop=True))
    out.to_csv(cache, index=False)
    return out


def fv_now(pair: str, now_ts: int, lookback_s: int = 1200, fresh_s: int = 180) -> dict:
    """Live fair value as of `now_ts` for the live tool (live_sim_plan.html S0). Pulls the most recent
    1-min FV bars over [now-lookback, now] (NEVER cached — always fresh) and returns the latest bar.

    Returns {fv, bar_ts, age_s, stale}. stale=True when the newest bar is older than fresh_s (Pyth FX
    pauses on weekends / holidays — the MarketHoursBreaker case): the caller must NOT treat fv as truth,
    only mark on the onchain mid. Returns {fv:None, stale:True, reason:...} on an empty/failed pull."""
    if pair not in PYTH_SYMBOL:
        raise ValueError(f"no Pyth symbol mapped for {pair}; add it to PYTH_SYMBOL")
    symbol, invert = PYTH_SYMBOL[pair]
    try:
        d = _get(symbol, int(now_ts) - lookback_s, int(now_ts), "1")
    except Exception as e:                       # network / shim failure -> treat as stale, don't guess
        return {"fv": None, "bar_ts": None, "age_s": None, "stale": True, "reason": f"fetch_error:{e}"}
    if d.get("s") != "ok" or not d.get("t"):
        return {"fv": None, "bar_ts": None, "age_s": None, "stale": True, "reason": "no_bars (FX-closed?)"}
    t_last = int(d["t"][-1])
    c_last = float(d["c"][-1])
    fv = (1.0 / c_last) if invert else c_last
    age = int(now_ts) - t_last
    return {"fv": fv, "bar_ts": t_last, "age_s": age, "stale": age > fresh_s,
            "reason": "fresh" if age <= fresh_s else f"stale {age}s > {fresh_s}s"}


def fv_at(fv: "pd.DataFrame", ts: "pd.Series") -> "pd.Series":
    """As-of join: fair value in effect at each fill ts (last bar at or before ts). Vectorized merge_asof.
    Returns a float Series aligned to ts.index."""
    import pandas as pd
    left = pd.DataFrame({"ts": ts.astype("int64").values}, index=ts.index).sort_values("ts")
    right = fv.sort_values("ts")
    merged = pd.merge_asof(left, right, on="ts", direction="backward")
    return merged.set_index(left.index)["fv"].reindex(ts.index)


if __name__ == "__main__":
    # smoke test against the cached tape window
    import sys
    pair = sys.argv[1] if len(sys.argv) > 1 else "eur/usd"
    tape = pd.read_parquet(ART / f"tape/tape_{pair.replace('/', '_')}_2026-06-15.parquet")
    s, e = int(tape["ts"].min()), int(tape["ts"].max())
    fv = fetch_fv(pair, s, e, reuse=False)
    print(f"{pair}: {len(fv)} 1-min FV bars over [{s}, {e}]")
    print(f"  fv range {fv['fv'].min():.5f} .. {fv['fv'].max():.5f}  (tape p {tape['p'].min():.5f} .. {tape['p'].max():.5f})")
    print(fv.head(3).to_string(index=False))
