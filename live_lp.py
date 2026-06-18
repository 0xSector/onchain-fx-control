# @purpose live_lp.py - live_sim_plan.html S1/S2: the FORWARD-PRESS LP tick. One stateful, crash-safe
#          process that loads state.json, polls the live Base feed (live_mid.py) + Pyth fair value, runs
#          the breaker, advances the held concentrated-liquidity position on the validated economics
#          (book.py primitives + sim.py Elsts geometry), runs the net-APR opportunity scanner across the
#          screened EUR fee tiers, recenters autonomously under the economic rule, then persists state and
#          appends the ledger. Each tick is a stateless process; the STATE is the file -> laptop-sleep /
#          crash safe, cloud-cron runnable with zero secrets.
#
#          This is the OPPOSITE of a backtest: sim.py/book.py measured what WOULD have happened over a
#          frozen week; this turns the LP ON and presses it forward against live feeds, marking to market
#          in real time. The economics are REUSED unchanged and adversarially reviewed (2026-06-15). The
#          live layer adds only: LiveFeed (RPC), on-disk state, the scanner, and the breaker.
#
#          Adversarial-review fixes folded in (live_sim_plan rev. e):
#            F1  mark on observe() 5-min TWAP, slot0 only for the breaker spot check (live_mid.twap_mid)
#            F2  fee_share in v3 L-units (our raw L vs live liquidity()), NOT a dollar-TVL proxy
#            F3  sigma shrinks toward run-realized TWAP vol as the window grows (parametric form kept)
#            F4  basis charged as a DISCRETE ledger event at (re)allocation, never a perpetual APR
#            F5  eth_getLogs over [last_block, now] -> self-healing interval volume
#            F7  per-corridor breaker: N-sigma over the trailing basis median, not a global 25bp
#            F8  TWO baselines marked every tick: HODL-the-inception-basket (IL peer) + Aave-USDC
#
#          Read-only public market data. PAPER only - no capital, no on-chain actions. NAV is notional.

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import live_mid
import pyth
# Self-contained, pandas-free economics (vendored verbatim from the validated book.py/sim.py) so the live
# tool runs in the public repo with zero pip installs and no private-pipeline import. See live_econ.py.
from live_econ import (cap_efficiency, lvr_apr, lp_amounts, lp_value, L_for_deposit, hodl_value,
                       REF_RANGE, BOOK_USD, MIN_TVL, MIN_VOL_WK, SEC_Y, AAVE_USDC_APR)

ROOT = Path(__file__).resolve().parent
# Output dir is configurable so the SAME code runs both in the private repo (default artifacts path) and
# at the public onchain-fx-control repo root (ONCHAIN_FX_LIVE_DIR=.). State + ledger + UI all land here.
LIVE = Path(os.environ.get("ONCHAIN_FX_LIVE_DIR", ROOT / "artifacts" / "book" / "live")).resolve()
LIVE.mkdir(parents=True, exist_ok=True)
STATE = LIVE / "state.json"
LEDGER = LIVE / "live_ledger.jsonl"

# ---- model / policy constants ----------------------------------------------------------------
CORRIDOR = "eur/usd"                # v1 launch scope: EUR/USDC on Base only (deepest, cleanest floor)
HALF_RANGE = REF_RANGE              # fixed +/-3% reference band (book.py headline band); v1 does NOT
#                                     let the scanner exploit the band degeneracy - it ranks POOLS only.
SIGMA_PRIOR = 0.071                 # EUR/USD 2y realized ann vol (book.realized_vol); refined at init
SIGMA_N0 = 200                      # shrinkage weight: trust live run-vol only after ~200 marks (F3)
VOL_HALFLIFE_S = 6 * 3600.0         # EWMA half-life for the per-pool live volume estimate
L_HALFLIFE_S = 3600.0               # EWMA half-life for in-range liquidity() — a SINGLE block's L_active is
#   noisy (a 30bp pool's active-tick L swings ~30x as concentrated positions cross the tick). The correct
#   denominator for interval fee attribution is the dwell-time-weighted AVERAGE in-range L, so we smooth it.
# snapshot weekly-volume priors (m2_pool_volume_7d, 2026-06-10) that seed the EWMA on first sight
VOL_PRIOR_WK = {"aero_e846": 33.3e6, "aero_f39b": 33.4e6, "aero_c5e5": 23.6e6,
                "pancake_1ca4": 3.81e6, "aero_183c": 139_269.0, "alien_7b2c": 486_125.0,
                "uni_7279": 636_276.0, "pancake_f0c5": 1.435e6, "uni_03d8": 3_549.0}
# snapshot per-pool TVL (m3_tvl_by_pool_7d, 2026-06-10) for the investability screen + fee-share cap.
POOL_TVL = {"aero_e846": 2_865_536.0, "aero_f39b": 591_996.0, "aero_c5e5": 219_423.0,
            "pancake_1ca4": 189_867.0, "aero_183c": 140_511.0, "alien_7b2c": 112_294.0,
            "uni_7279": 93_723.0, "pancake_f0c5": 43_429.0, "uni_03d8": 11_955.0}

# WATCH set = all 3 Aerodrome EUR/USDC tiers (the clean slot0/observe-readable Base EUR pools). Every tick
# polls and RANKS all of them so they're visible in the UI. (The other 16 Base EUR pools are thin: the
# Uniswap ones are <$100k TVL, and Tessera's $23.5M/wk is a prop-AMM with no v3 mid — excluded by design.)
CANDIDATES = list(live_mid.POOLS)
# ALLOCATION screen (book.MIN_TVL/MIN_VOL_WK): the book may only be deployed into pools deep enough to be a
# real LP venue. This keeps aero_c5e5 ($219k TVL < $300k floor) WATCHED but allocation-INELIGIBLE — its
# ~$29M/wk over $219k TVL is wash/arb churn (implied >1000% pool yield), NOT fee flow a $10k LP can earn;
# allocating the book there would capture NAV on a garbage number. To make it allocatable, add it to
# ELIGIBLE (or lower MIN_TVL). Candidate-set refresh is the weekly snapshot's job (plan), not per-tick.
ELIGIBLE = [l for l in CANDIDATES
            if POOL_TVL.get(l, 0) >= MIN_TVL and VOL_PRIOR_WK.get(l, 0) >= MIN_VOL_WK]
MID_POOL = "aero_e846"              # canonical deepest-TVL pool for the breaker spot reference

# autonomous-action policy
GAS_UNITS = 250_000                 # per recenter (remove+add); realloc = 2x (exit+enter)
ETH_USD = 3000.0                    # constant ETH price for gas->USD (FLAGGED; gas is ~$0.005 on Base)
GAS_FLOOR_USD = 0.02                # plan's low end of the Base recenter cost range
BASIS_REENTRY_BPS = 0.0             # within-Base EUR realloc keeps EURC+USDC legs (no FX leg swap) -> ~0
#                                     (cross-currency / cross-chain realloc adds basis+bridge; deferred S5)
PAYBACK_DAYS = 30.0                 # D7: a realloc must repay its switch cost within this window
HYSTERESIS_APR = 0.005             # D7: and beat the held pool's net by >=50bps (a band, not a hair)

# breaker (F7)
BASIS_HIST_MAX = 300                # trailing spot-vs-Pyth dev window (bps)
DISLOC_FLOOR_BPS = 25.0             # never flag tighter than this (EUR S2 max ~6bp -> a global 25 never fires)
DISLOC_NSIGMA = 4.0                 # flag at median + N*sigma over the trailing basis
STALE_TICK_MULT = 3.0               # a tick gap > this x the nominal cadence is a liveness concern (UI lamp)
NOMINAL_CADENCE_S = 900             # D2: 15-min nominal tick


# ---- v3 raw-L fee share (F2) -----------------------------------------------------------------
def raw_L(usd: float, p: float, p_a: float, p_b: float, dec0: int = 6, dec1: int = 6) -> float:
    """Our position's liquidity L in the POOL's native (sqrtPriceX96) units, so it is comparable to the
    on-chain liquidity() read. Derived from the dollar deposit via the standard v3 amount<->L relation:
        L = amount1_raw / (sqrtP - sqrtP_a)   (token1 leg)   or   amount0_raw * sqrtP*sqrtP_b/(sqrtP_b-sqrtP).
    Equal 6-decimal tokens -> price_raw == price_human. Both legs agree in-range (validated)."""
    Ln = L_for_deposit(usd, p, p_a, p_b)
    x0, y0 = lp_amounts(Ln, p, p_a, p_b)
    spP, spA, spB = math.sqrt(p), math.sqrt(p_a), math.sqrt(p_b)
    if y0 > 1e-9 and spP > spA:
        return y0 * 10 ** dec1 / (spP - spA)
    if x0 > 1e-9 and spB > spP:
        return x0 * 10 ** dec0 * (spP * spB) / (spB - spP)
    return 0.0


def fee_share_Lunit(usd: float, mid: float, L_active: float, pool_tvl: float = 0.0,
                    half: float = HALF_RANGE) -> float:
    """Our in-range share of fee flow: ourL / (ourL + L_active). Wider band -> thinner ourL/tick ->
    smaller share (the F2 dilution the dollar-TVL proxy misses). CAPPED at the capital-weighted share
    deposit/(deposit+pool_TVL): the L-unit share refines DOWNWARD from that ceiling (band dilution), so a
    share above it means the active tick is transiently thinner than the pool average — a spike, not edge."""
    lr = raw_L(usd, mid, mid * (1 - half), mid * (1 + half))
    share = lr / (lr + L_active) if (lr + L_active) > 0 else 0.0
    if pool_tvl > 0:
        share = min(share, usd / (usd + pool_tvl))
    return share


def modeled_net_apr(usd: float, mid: float, fee_bps: float, vol_yr: float, L_active: float,
                    sigma: float, pool_tvl: float = 0.0, half: float = HALF_RANGE) -> dict:
    """The scanner's ranking metric: modeled NET APR = gross_fee_APR - LVR, at the reference band.
    gross = annualized_volume * fee_tier * L-unit_share / deposit. LVR = parametric @ sigma. Basis is
    NOT here (it is a discrete switch cost at realloc, F4)."""
    share = fee_share_Lunit(usd, mid, L_active, pool_tvl, half)
    gross = vol_yr * (fee_bps / 1e4) * share / usd if usd > 0 else 0.0
    lvr = lvr_apr(sigma, half)
    return {"gross_apr": gross, "lvr_apr": lvr, "net_apr": gross - lvr, "fee_share": share}


# ---- gas / cost ------------------------------------------------------------------------------
def gas_usd(gas_gwei: float, mult: float = 1.0) -> float:
    if gas_gwei != gas_gwei:                          # NaN -> use the floor
        return GAS_FLOOR_USD * mult
    raw = gas_gwei * 1e-9 * GAS_UNITS * ETH_USD * mult
    return max(GAS_FLOOR_USD * mult, raw)


# ---- sigma (F3) ------------------------------------------------------------------------------
def sigma_now(sig: dict) -> float:
    """Blend the 2y prior with the live run-realized TWAP vol, weighting live as the window grows."""
    n = sig.get("n", 0)
    sum_r2, sum_dt = sig.get("sum_r2", 0.0), sig.get("sum_dt", 0.0)
    run = math.sqrt(sum_r2 / sum_dt) if sum_dt > 0 else sig["prior"]
    w = n / (n + SIGMA_N0)
    return w * run + (1 - w) * sig["prior"]


def sigma_update(sig: dict, mid: float, ts: float) -> None:
    last_mid, last_ts = sig.get("last_mid"), sig.get("last_ts")
    if last_mid and last_ts and mid > 0 and ts > last_ts:
        r = math.log(mid / last_mid)
        dt_y = (ts - last_ts) / SEC_Y
        if dt_y > 0:
            sig["sum_r2"] = sig.get("sum_r2", 0.0) + r * r
            sig["sum_dt"] = sig.get("sum_dt", 0.0) + dt_y
            sig["n"] = sig.get("n", 0) + 1
    sig["last_mid"], sig["last_ts"] = mid, ts


# ---- volume EWMA -----------------------------------------------------------------------------
def vol_yr_for(state: dict, label: str) -> float:
    return state["vol_ewma"].get(label, {}).get("rate_yr", VOL_PRIOR_WK.get(label, 1e6) * 52.0)


def vol_update(state: dict, label: str, interval_vol: float, dt_s: float, first_sight: bool) -> None:
    e = state["vol_ewma"].setdefault(label, {"rate_yr": VOL_PRIOR_WK.get(label, 1e6) * 52.0, "n": 0})
    if first_sight or dt_s <= 0:                       # no real window yet -> keep the prior
        return
    obs_rate_yr = interval_vol / (dt_s / SEC_Y)
    alpha = 1.0 - math.exp(-dt_s / VOL_HALFLIFE_S)
    e["rate_yr"] = alpha * obs_rate_yr + (1 - alpha) * e["rate_yr"]
    e["n"] += 1


def L_active_for(state: dict, label: str, live: float | None = None) -> float:
    """Smoothed in-range liquidity for label. Falls back to the live read (or a large sentinel) before the
    EWMA is seeded so fee_share can never divide by zero or spike on a single thin-L block."""
    e = state.setdefault("L_ewma", {}).get(label)
    if e and e.get("L", 0) > 0:
        return e["L"]
    return live if (live and live > 0) else 1e15


def L_update(state: dict, label: str, L_live: float, dt_s: float) -> None:
    le = state.setdefault("L_ewma", {})
    e = le.get(label)
    if not L_live or L_live <= 0:
        return
    if not e:                                          # seed
        le[label] = {"L": float(L_live), "n": 1}
        return
    alpha = 1.0 - math.exp(-max(0, dt_s) / L_HALFLIFE_S) if dt_s > 0 else 0.0
    e["L"] = alpha * L_live + (1 - alpha) * e["L"]
    e["n"] += 1


# ---- state -----------------------------------------------------------------------------------
def init_state(now_ts: int) -> dict:
    # sigma prior = EUR/USD 2y realized ann vol (book.realized_vol, ECB daily). Baked as a constant so the
    # public/cloud runtime needs no ECB pull or pandas; refresh SIGMA_PRIOR from book.py at each re-weight.
    prior = SIGMA_PRIOR
    return {
        "inception_ts": now_ts, "book_usd": BOOK_USD, "corridor": CORRIDOR,
        "position": None,                              # opened on the first tick by the scanner
        "inception": None,                             # frozen HODL peer basket {x0,y0,mid}
        "cursors": {}, "vol_ewma": {}, "L_ewma": {},
        "sigma": {"prior": prior, "n": 0, "sum_r2": 0.0, "sum_dt": 0.0, "last_mid": None, "last_ts": None},
        "basis_hist": [], "nav_hist": [], "n_reallocations": 0, "n_ticks": 0,
        "breaker": {"state": "INIT", "reason": "no ticks yet", "since": now_ts},
        "last_tick_ts": None,
    }


def load_state(now_ts: int) -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return init_state(now_ts)


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=2, default=float))


LEDGER_MAX = 2500                   # cap the on-disk ledger so the committed public repo stays bounded
#                                     (UI reads only the last ~45; the equity curve lives in state.nav_hist).


def append_ledger(row: dict) -> None:
    with LEDGER.open("a") as f:
        f.write(json.dumps(row, default=float) + "\n")
    # trim to the last LEDGER_MAX lines (cheap; only rewrites when it actually grows past the cap)
    try:
        lines = LEDGER.read_text().splitlines()
        if len(lines) > LEDGER_MAX:
            LEDGER.write_text("\n".join(lines[-LEDGER_MAX:]) + "\n")
    except OSError:
        pass


# ---- breaker (F7) ----------------------------------------------------------------------------
def assess_breaker(state: dict, feed: dict, fvn: dict, held_label: str | None) -> dict:
    """Return {state, reason, dev_bps, thresh_bps}. State priority: STALE > DISLOCATED > FX_CLOSED > OK.
       STALE      = the feed we need is broken -> freeze entirely (no advance).
       FX_CLOSED  = Pyth FX paused (weekend/holiday) -> mark on the onchain mid, sigma falls to prior,
                    skip the dislocation check and basis update; no reallocation.
       DISLOCATED = onchain spot diverges from fair value beyond the per-corridor threshold -> HOLD the
                    range (advance accounting, but no recenter, no realloc).
       OK         = full autonomy."""
    # STALE: any candidate (and especially the held) pool's reads failed
    bad = [l for l, o in feed["pools"].items() if not o["ok"]]
    if (held_label and not feed["pools"].get(held_label, {}).get("ok")) or len(bad) == len(CANDIDATES):
        return {"state": "STALE", "reason": f"feed read failed: {bad}", "dev_bps": None, "thresh_bps": None}
    if fvn.get("stale"):
        return {"state": "FX_CLOSED", "reason": fvn.get("reason", "FX-closed"), "dev_bps": None, "thresh_bps": None}
    # DISLOCATED: held (or canonical) spot vs Pyth, per-corridor N-sigma over the trailing basis
    ref = held_label or MID_POOL
    o = feed["pools"].get(ref, {})
    dev = (o.get("spot_mid", float("nan")) / fvn["fv"] - 1.0) * 1e4 if fvn.get("fv") else float("nan")
    hist = state.get("basis_hist", [])
    if len(hist) >= 10:
        med = sorted(hist)[len(hist) // 2]
        mean = sum(hist) / len(hist)
        sd = (sum((x - mean) ** 2 for x in hist) / len(hist)) ** 0.5
        thresh = max(DISLOC_FLOOR_BPS, abs(med) + DISLOC_NSIGMA * sd)
    else:
        thresh = DISLOC_FLOOR_BPS
    if dev == dev and abs(dev) > thresh:
        return {"state": "DISLOCATED", "reason": f"spot {dev:+.1f}bp vs FV > {thresh:.1f}bp",
                "dev_bps": dev, "thresh_bps": thresh}
    return {"state": "OK", "reason": "fresh; spot~FV", "dev_bps": dev, "thresh_bps": thresh}


# ---- position open / advance / recenter / realloc --------------------------------------------
def open_position(state: dict, label: str, mid: float, now_ts: int) -> dict:
    half = HALF_RANGE
    p_a, p_b = mid * (1 - half), mid * (1 + half)
    Ln = L_for_deposit(state["book_usd"], mid, p_a, p_b)
    x0, y0 = lp_amounts(Ln, mid, p_a, p_b)
    return {"pool": label, "deposit_usd": state["book_usd"], "half_range": half,
            "p_a": p_a, "p_b": p_b, "L_norm": Ln, "x0": x0, "y0": y0,
            "entry_mid": mid, "entry_ts": now_ts, "last_ts": now_ts,
            "fees_usd": 0.0, "lvr_usd": 0.0, "gas_usd": 0.0,
            "fees_since_recenter": 0.0, "n_recenters": 0, "ticks_in_range": 0, "n_ticks": 0}


def advance(state: dict, pos: dict, feed: dict, sig_val: float, now_ts: int, allow_recenter: bool) -> dict:
    """Mark + accrue one tick on the held position. Returns a per-tick accrual record."""
    o = feed["pools"][pos["pool"]]
    mid = o["mark_mid"]
    p_a, p_b, half = pos["p_a"], pos["p_b"], pos["half_range"]
    in_range = p_a <= mid <= p_b
    dt_y = max(0.0, (now_ts - pos["last_ts"]) / SEC_Y)
    V = lp_value(pos["L_norm"], mid, p_a, p_b)

    fee = lvr = 0.0
    if in_range:
        share = fee_share_Lunit(pos["deposit_usd"], mid, L_active_for(state, pos["pool"], o["L_active"]),
                                POOL_TVL.get(pos["pool"], 0.0), half)
        fee = (o["fee_bps"] / 1e4) * o["interval_vol_usd"] * share
        lvr = lvr_apr(sig_val, half) * V * dt_y
        pos["ticks_in_range"] += 1
        pos["fees_since_recenter"] += fee
    pos["fees_usd"] += fee
    pos["lvr_usd"] += lvr
    pos["n_ticks"] += 1

    recenter = None
    if allow_recenter and not in_range and pos["fees_since_recenter"] > gas_usd(feed["gas_gwei"]):
        recenter = do_recenter(pos, mid, feed["gas_gwei"])

    pos["last_ts"] = now_ts
    return {"mid": mid, "in_range": in_range, "fee": fee, "lvr": lvr, "V": V, "recenter": recenter}


def do_recenter(pos: dict, mid: float, gas_gwei: float) -> dict:
    """Re-center the band on the live mid. Capital conservation: value the OLD band first, net gas,
    redeposit into the new band (sim.PassiveLP._recenter, F10 backward-looking economic rule)."""
    g = gas_usd(gas_gwei)
    v_withdrawn = lp_value(pos["L_norm"], mid, pos["p_a"], pos["p_b"])
    half = pos["half_range"]
    pos["p_a"], pos["p_b"] = mid * (1 - half), mid * (1 + half)
    # redeposit the FULL withdrawn value; gas is the gas_usd ledger line that NAV subtracts once
    # (do NOT also net it out of L here — that would double-charge the recenter, NAV = V + fees - gas).
    pos["L_norm"] = L_for_deposit(v_withdrawn, mid, pos["p_a"], pos["p_b"])
    pos["x0"], pos["y0"] = lp_amounts(pos["L_norm"], mid, pos["p_a"], pos["p_b"])
    pos["gas_usd"] += g
    pos["n_recenters"] += 1
    pos["fees_since_recenter"] = 0.0
    return {"new_p_a": pos["p_a"], "new_p_b": pos["p_b"], "gas": g, "at_mid": mid}


def scan(state: dict, feed: dict, sig_val: float, held: str) -> dict:
    """Rank every candidate by modeled net APR at its live mid/L/volume. Returns the ranking + the
    realloc decision against the held pool (switch-cost hurdle + payback + hysteresis, D7)."""
    usd = state["book_usd"]
    rank = []
    for lbl in CANDIDATES:
        o = feed["pools"].get(lbl)
        if not o or not o["ok"]:
            continue
        m = modeled_net_apr(usd, o["mark_mid"], o["fee_bps"], vol_yr_for(state, lbl),
                            L_active_for(state, lbl, o["L_active"]), sig_val, POOL_TVL.get(lbl, 0.0))
        rank.append({"pool": lbl, "fee_bps": o["fee_bps"], "eligible": lbl in ELIGIBLE, **m})
    rank.sort(key=lambda r: r["net_apr"], reverse=True)
    # the book is only allocated among ELIGIBLE pools; ineligible (screened) pools are ranked for VISIBILITY
    # but never become `best`, so the scanner can't reallocate the paper book into a wash-inflated thin pool.
    elig = [r for r in rank if r["eligible"]]
    decision = {"ranking": rank, "best": elig[0]["pool"] if elig else None, "reallocate": False}
    if not elig:
        return decision
    best = elig[0]
    net_now = next((r["net_apr"] for r in rank if r["pool"] == held), None)
    decision["net_now"] = net_now
    decision["net_best"] = best["net_apr"]
    if best["pool"] != held and net_now is not None:
        gain_apr = best["net_apr"] - net_now
        # switch cost: gas out + gas in + LVR realized on exit (approx one parametric tick of the band,
        # immaterial at $10k) + basis re-entry (0 within-Base EUR). Priced as a one-time dollar drag.
        switch = gas_usd(feed["gas_gwei"], mult=2.0) + (BASIS_REENTRY_BPS / 1e4) * usd
        payback_gain = gain_apr * usd * (PAYBACK_DAYS / 365.0)
        decision.update({"gain_apr": gain_apr, "switch_cost": switch, "payback_gain": payback_gain})
        if gain_apr > HYSTERESIS_APR and payback_gain > switch:
            decision["reallocate"] = True
    return decision


def do_reallocate(state: dict, old_pos: dict, feed: dict, to_label: str) -> dict:
    """Realize the held position, pay the switch cost, open a fresh band on the new pool's live mid.
    Paper: NAV carries forward minus the switch cost. Inception HODL basket is unchanged."""
    g = gas_usd(feed["gas_gwei"], mult=2.0)
    o = feed["pools"][to_label]
    new_mid = o["mark_mid"]
    v_now = lp_value(old_pos["L_norm"], feed["pools"][old_pos["pool"]]["mark_mid"],
                     old_pos["p_a"], old_pos["p_b"])
    carry_fees = old_pos["fees_usd"]
    carry_lvr = old_pos["lvr_usd"]
    carry_gas = old_pos["gas_usd"] + g
    basis_cost = (BASIS_REENTRY_BPS / 1e4) * old_pos["deposit_usd"]
    half = old_pos["half_range"]
    p_a, p_b = new_mid * (1 - half), new_mid * (1 + half)
    # redeposit the marked value minus the basis re-entry drag (a principal cost, not gas). Gas is
    # carried in carry_gas below and subtracted once by NAV = V + fees - gas (no double-charge).
    redeposit = v_now - basis_cost
    Ln = L_for_deposit(redeposit, new_mid, p_a, p_b)
    x0, y0 = lp_amounts(Ln, new_mid, p_a, p_b)
    new_pos = {"pool": to_label, "deposit_usd": old_pos["deposit_usd"], "half_range": half,
               "p_a": p_a, "p_b": p_b, "L_norm": Ln, "x0": x0, "y0": y0,
               "entry_mid": new_mid, "entry_ts": old_pos["last_ts"], "last_ts": old_pos["last_ts"],
               # carry cumulative cost ledgers so NAV is continuous across the move
               "fees_usd": carry_fees, "lvr_usd": carry_lvr, "gas_usd": carry_gas,
               "fees_since_recenter": 0.0, "n_recenters": old_pos["n_recenters"],
               "ticks_in_range": old_pos["ticks_in_range"], "n_ticks": old_pos["n_ticks"]}
    state["n_reallocations"] += 1
    return {"pos": new_pos, "from": old_pos["pool"], "to": to_label, "gas": g,
            "basis_cost": basis_cost, "at_mid": new_mid}


# ---- NAV / baselines -------------------------------------------------------------------------
def nav_marks(state: dict, pos: dict, mid: float, now_ts: int) -> dict:
    """The headline + two baselines (F8). NAV = marked LP value + fees - gas (realized IL is implicit in
    the LP mark vs HODL). Parametric LVR is a SEPARATE diagnostic, never subtracted from NAV (would
    double-count the realized IL)."""
    V = lp_value(pos["L_norm"], mid, pos["p_a"], pos["p_b"])
    nav = V + pos["fees_usd"] - pos["gas_usd"]
    inc = state["inception"]
    hodl = hodl_value(inc["x0"], inc["y0"], mid)                       # IL peer: inception basket marked now
    yrs = (now_ts - state["inception_ts"]) / SEC_Y
    aave = state["book_usd"] * (1 + AAVE_USDC_APR * yrs)
    return {"nav": nav, "V": V, "hodl": hodl, "aave": aave, "lvr_diag": pos["lvr_usd"],
            "fees": pos["fees_usd"], "gas": pos["gas_usd"], "yrs": yrs}


# ---- the tick --------------------------------------------------------------------------------
def tick() -> dict:
    # load state FIRST so the persisted per-pool block cursors flow into the feed -> the eth_getLogs
    # window is [last_block, now] (real interval volume), not a zero-width first-sight window every tick.
    prev = json.loads(STATE.read_text()) if STATE.exists() else None
    cursors = prev.get("cursors") if prev else {}
    feed = live_mid.poll(CANDIDATES, cursors)
    now_ts = feed["block_ts"]
    fvn = pyth.fv_now(CORRIDOR, now_ts)
    state = prev if prev is not None else init_state(now_ts)
    state["n_ticks"] += 1
    held = state["position"]["pool"] if state["position"] else None
    br = assess_breaker(state, feed, fvn, held)
    actions = []
    accrual = None

    if br["state"] == "STALE":
        # freeze entirely: do not advance on bad data, just record and exit
        actions.append({"type": "FREEZE", "reason": br["reason"]})
    else:
        # update sigma + per-pool volume EWMA from this tick (uses each pool's own interval)
        sigma_update(state["sigma"], feed["pools"].get(held or MID_POOL, {}).get("mark_mid", 0) or
                     feed["pools"][MID_POOL]["mark_mid"], now_ts)
        for lbl in CANDIDATES:
            o = feed["pools"].get(lbl, {})
            if o.get("ok"):
                dt_s = (now_ts - state["last_tick_ts"]) if state["last_tick_ts"] else 0
                vol_update(state, lbl, o.get("interval_vol_usd", 0.0), dt_s, o.get("first_sight", True))
                L_update(state, lbl, o.get("L_active", 0.0), dt_s)
            state["cursors"][lbl] = o.get("to_block", state["cursors"].get(lbl))
        sig_val = sigma_now(state["sigma"]) if br["state"] != "FX_CLOSED" else state["sigma"]["prior"]

        if state["position"] is None:
            # INCEPTION: scan and allocate the book to the best screened pool
            dec = scan(state, feed, sig_val, held="")
            best = dec["best"] or MID_POOL
            mid0 = feed["pools"][best]["mark_mid"]
            state["position"] = open_position(state, best, mid0, now_ts)
            inc_x0, inc_y0 = state["position"]["x0"], state["position"]["y0"]
            state["inception"] = {"x0": inc_x0, "y0": inc_y0, "mid": mid0}
            actions.append({"type": "ALLOCATE", "pool": best, "mid": mid0,
                            "net_apr": next((r["net_apr"] for r in dec["ranking"] if r["pool"] == best), None),
                            "ranking": dec["ranking"]})
        else:
            pos = state["position"]
            allow_recenter = br["state"] in ("OK", "FX_CLOSED")
            accrual = advance(state, pos, feed, sig_val, now_ts, allow_recenter)
            if accrual["recenter"]:
                actions.append({"type": "RECENTER", "pool": pos["pool"], **accrual["recenter"]})
            # scanner only with full autonomy (OK). DISLOCATED/FX_CLOSED hold.
            if br["state"] == "OK":
                dec = scan(state, feed, sig_val, held=pos["pool"])
                if dec["reallocate"]:
                    r = do_reallocate(state, pos, feed, dec["best"])
                    state["position"] = r["pos"]
                    actions.append({"type": "REALLOCATE", **{k: r[k] for k in ("from", "to", "gas", "basis_cost", "at_mid")},
                                    "gain_apr": dec.get("gain_apr"), "ranking": dec["ranking"]})
                else:
                    actions.append({"type": "SCAN_DECLINE", "best": dec["best"], "held": pos["pool"],
                                    "net_now": dec.get("net_now"), "net_best": dec.get("net_best"),
                                    "ranking": dec["ranking"]})

        # update the trailing basis history (only when FV is trustworthy)
        if br["state"] in ("OK", "DISLOCATED") and br.get("dev_bps") == br.get("dev_bps") and br.get("dev_bps") is not None:
            state["basis_hist"] = (state["basis_hist"] + [br["dev_bps"]])[-BASIS_HIST_MAX:]

    # ---- NAV marks (every non-failed tick that has a position) ----
    marks = None
    if state["position"] is not None and br["state"] != "STALE":
        mid = feed["pools"][state["position"]["pool"]]["mark_mid"]
        marks = nav_marks(state, state["position"], mid, now_ts)
        state["nav_hist"] = (state["nav_hist"] + [{
            "ts": now_ts, "nav": marks["nav"], "hodl": marks["hodl"], "aave": marks["aave"],
            "mid": mid, "breaker": br["state"]}])[-5000:]

    state["breaker"] = {**br, "since": now_ts if state["breaker"]["state"] != br["state"] else state["breaker"]["since"]}
    state["last_tick_ts"] = now_ts
    save_state(state)

    # ---- ledger row ----
    pos = state["position"]
    row = {"ts": now_ts, "block": feed["block"], "gas_gwei": feed["gas_gwei"], "breaker": br["state"],
           "breaker_reason": br["reason"], "fv": fvn.get("fv"), "fv_stale": fvn.get("stale"),
           "dev_bps": br.get("dev_bps"), "sigma": sigma_now(state["sigma"]),
           "held_pool": pos["pool"] if pos else None,
           "mid": (feed["pools"][pos["pool"]]["mark_mid"] if pos else None),
           "in_range": (accrual["in_range"] if accrual else None),
           "tick_fee": (accrual["fee"] if accrual else None),
           "tick_lvr": (accrual["lvr"] if accrual else None),
           "nav": (marks["nav"] if marks else None), "hodl": (marks["hodl"] if marks else None),
           "aave": (marks["aave"] if marks else None),
           "fees_cum": pos["fees_usd"] if pos else None, "lvr_cum": pos["lvr_usd"] if pos else None,
           "gas_cum": pos["gas_usd"] if pos else None, "n_recenters": pos["n_recenters"] if pos else None,
           "n_reallocations": state["n_reallocations"],
           "actions": actions}
    append_ledger(row)

    # ---- render ----
    try:
        import live_render
        live_render.render(state, row)
    except Exception as e:
        print(f"  [render skipped: {e}]")

    return {"state": state, "row": row, "feed": feed, "fvn": fvn, "marks": marks, "actions": actions}


def _print_tick(res: dict) -> None:
    r, m, st = res["row"], res["marks"], res["state"]
    print(f"[{datetime.fromtimestamp(r['ts'], timezone.utc):%Y-%m-%d %H:%M:%SZ}] "
          f"block {r['block']} · breaker {r['breaker']} · sigma {r['sigma']*100:.1f}%")
    for a in res["actions"]:
        if a["type"] in ("ALLOCATE", "REALLOCATE"):
            rk = " | ".join(f"{x['pool']} {x['net_apr']*100:+.1f}%" for x in a.get("ranking", []))
            tgt = a.get("pool") or a.get("to")
            print(f"   >> {a['type']} -> {tgt}   [{rk}]")
        elif a["type"] == "RECENTER":
            print(f"   >> RECENTER {a['pool']} @ {a['at_mid']:.5f} (gas ${a['gas']:.3f})")
        elif a["type"] == "SCAN_DECLINE":
            rk = " | ".join(f"{x['pool']} {x['net_apr']*100:+.1f}%" for x in a.get("ranking", []))
            print(f"   .. scan: hold {a['held']} (net {(a['net_now'] or 0)*100:+.1f}%); best {a['best']}  [{rk}]")
        elif a["type"] == "FREEZE":
            print(f"   !! FREEZE: {a['reason']}")
    if m:
        nav, hodl, aave = m["nav"], m["hodl"], m["aave"]
        print(f"   NAV ${nav:,.2f}  vs HODL ${hodl:,.2f} ({(nav-hodl):+,.2f})  "
              f"vs Aave ${aave:,.2f} ({(nav-aave):+,.2f})  · fees ${m['fees']:.3f}  LVR-diag ${m['lvr_diag']:.3f}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if cmd == "reset":
        for p in (STATE, LEDGER):
            if p.exists():
                p.unlink()
        print(f"reset: cleared {STATE.name}, {LEDGER.name}")
        return
    if cmd == "status":
        if not STATE.exists():
            print("no state yet")
            return
        s = json.loads(STATE.read_text())
        pos = s.get("position")
        print(f"inception {datetime.fromtimestamp(s['inception_ts'], timezone.utc):%Y-%m-%d %H:%MZ} · "
              f"ticks {s['n_ticks']} · reallocations {s['n_reallocations']} · breaker {s['breaker']['state']}")
        if pos:
            print(f"held {pos['pool']} · band [{pos['p_a']:.4f},{pos['p_b']:.4f}] · fees ${pos['fees_usd']:.3f} · "
                  f"LVR-diag ${pos['lvr_usd']:.3f} · recenters {pos['n_recenters']}")
        return
    res = tick()
    _print_tick(res)


if __name__ == "__main__":
    main()
