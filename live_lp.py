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
# Delta-hedge overlay reuses the self-tested EUR-delta math VERBATIM (delta_hedge.py is unchanged).
from delta_hedge import eur_delta_usd, v3_amounts, REHEDGE_COST_1PCT

ROOT = Path(__file__).resolve().parent
# Output dir is configurable so the SAME code runs both in the private repo (default artifacts path) and
# at the public onchain-fx-control repo root (ONCHAIN_FX_LIVE_DIR=.). State + ledger + UI all land here.
LIVE = Path(os.environ.get("ONCHAIN_FX_LIVE_DIR", ROOT / "artifacts" / "book" / "live")).resolve()
LIVE.mkdir(parents=True, exist_ok=True)
STATE = LIVE / "state.json"
LEDGER = LIVE / "live_ledger.jsonl"
HEDGE_EVAL = LIVE / "hedge_eval.jsonl"   # onchain_fx_hedge_001: live naked-vs-hedged Sharpe/vol (upserted)

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
# snapshot weekly-volume priors (m2_pool_volume_7d, 2026-06-10) that seed the EWMA on first sight.
# univ4_64db = Uniswap v4 EURC/USDC 5bp, measured ~$6.9k/28h on 2026-06-18 (probe_v4_pools.py) -> ~$42k/wk.
VOL_PRIOR_WK = {"aero_e846": 33.3e6, "aero_f39b": 33.4e6, "aero_c5e5": 23.6e6,
                "pancake_1ca4": 3.81e6, "aero_183c": 139_269.0, "alien_7b2c": 486_125.0,
                "uni_7279": 636_276.0, "pancake_f0c5": 1.435e6, "uni_03d8": 3_549.0,
                "univ4_64db": 42_000.0}
# snapshot per-pool TVL (m3_tvl_by_pool_7d, 2026-06-10) for the investability screen + fee-share cap.
# univ4_64db has NO snapshot TVL (v4 singleton, excluded from the balance-sum m3); ~$9k is an active-L
# proxy (its in-range L 3.42e11 vs aero_e846 1.06e14 at $2.87M TVL) — order-of-magnitude only, watch-only.
POOL_TVL = {"aero_e846": 2_865_536.0, "aero_f39b": 591_996.0, "aero_c5e5": 219_423.0,
            "pancake_1ca4": 189_867.0, "aero_183c": 140_511.0, "alien_7b2c": 112_294.0,
            "uni_7279": 93_723.0, "pancake_f0c5": 43_429.0, "uni_03d8": 11_955.0,
            "univ4_64db": 9_000.0}

# WATCH set = every readable Base EUR pool in live_mid.POOLS (9 v3-family + 1 Uniswap v4). Every tick polls
# and RANKS all of them so they're visible in the UI; only the deep, screen-passing ones are ALLOCATABLE.
# v4 (univ4_64db) is a singleton-poolId pool marked on spot (no oracle) and is thin -> watch-only by design.
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

# ---- delta-hedge overlay (delta_hedge_plan.html, 2026-06-18) ----------------------------------
# The overlay runs as a SEPARATE parallel book (its own ONCHAIN_FX_LIVE_DIR), toggled by this flag, so the
# original naked LP keeps running byte-identical. OFF (default) -> no hedge machinery touches the book at all
# (naked book, original behaviour). ON -> the short overlay + nav_hedged + the hedge UI. Run two cron steps:
# the naked book with the flag unset, and a hedged book with ONCHAIN_FX_HEDGE=1 + its own dir/out.
HEDGE_ON = os.environ.get("ONCHAIN_FX_HEDGE", "0").strip().lower() not in ("0", "", "false", "no", "off")
# A PAPER short-EUR leg sized to the LP's live $-delta (= the USD value of the EUR/EURC leg, x*p). The
# concentrated LP is born ~49% long EUR; that delta is an UNCOMPENSATED directional bet whose P&L
# dominates the book's variance. Shorting it isolates the fee-minus-LVR carry. This is a SHARPE improver,
# NOT a yield improver (net APR ~flat): it removes DELTA, never LVR (gamma is unhedgeable onchain). The
# overlay is ADDITIVE — nav/hodl/aave stay byte-identical; nav_hedged is a new line. Paper only, no venue.
REHEDGE_BAND = 0.01          # re-size the short when |EUR move since last sizing| > 1% (D1; the validated
#   delta_hedge.py band, "+/-1% EUR move", ~51 crossings/yr) — NOT a |residual|/book rule (see hedge_step).
# Paper resize cost in bps on the (re)sized notional, calibrated so the LIVE per-resize charge matches the
# OFFLINE delta_hedge.REHEDGE_COST_1PCT annual drag (D3): REHEDGE_COST_1PCT (0.0031/yr) = ~51 band
# crossings/yr * ~$0.62/resize on $10k. Per-resize $0.62 on the ~$4,900 (=0.49*book) notional = 1.24bp.
#   1.24bp = (REHEDGE_COST_1PCT * BOOK_USD / 51 crossings) / (0.49 * BOOK_USD) * 1e4.
# NB: delta_hedge_plan D3 prints "12.4bp" — that is a 10x decimal slip (12.4bp*$4,900 = $6.07 != $0.62 and
# would 10x-overcharge the hedge); 1.24bp is the value that makes live and offline agree, per D3's own rule.
PERP_FEE_BPS = round(REHEDGE_COST_1PCT * BOOK_USD / 51.0 / (0.49 * BOOK_USD) * 1e4, 2)   # -> 1.24
CARRY_RATE = float(os.environ.get("ONCHAIN_FX_HEDGE_CARRY", "0.0"))   # carry on the SHORT notional, annual.
#   DEFAULT 0.0 (book carry at 0; any + carry is upside, re-underwrite each snapshot). ONCHAIN_FX_HEDGE_CARRY
#   =0.0105 toggles the Ostium ~125bp-gap Jun-2026 scenario (flips the UI carry lamp amber). (D2)


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


# ---- delta-hedge overlay: size / mark / resize -----------------------------------------------
def _hedge_unrealized(h: dict, mid: float) -> float:
    """Mark-to-market P&L of the CURRENT short leg since it was last (re)sized. A short GAINS as EUR
    falls: pnl = notional * (entry_mid - mid) / entry_mid. Linear in mid -> cancels the LP's linear delta
    EXACTLY at the sizing instant; the LP leg's convexity (gamma/LVR) is what survives as a drag."""
    em = h.get("entry_mid")
    if not em or em <= 0:
        return 0.0
    return h["notional_usd"] * (em - mid) / em


def gamma_resid_usd(pos: dict, mid: float, band: float = REHEDGE_BAND) -> float:
    """Diagnostic the UI must surface: the convexity drag the hedge CANNOT remove, over a reference -band
    move. The short is linear, the LP leg convex, so gamma always survives (always < 0). PARAMETRIC from
    current geometry, NOT a realized P&L: dV(-band) - notional * (-band)."""
    L, p_a, p_b = pos["L_norm"], pos["p_a"], pos["p_b"]
    notional = eur_delta_usd(L, mid, p_a, p_b)
    midp = mid * (1 - band)
    dV = lp_value(L, midp, p_a, p_b) - lp_value(L, mid, p_a, p_b)
    return dV - notional * ((midp - mid) / mid)


def hedge_sign_selftest(pos: dict, mid: float) -> None:
    """Step-0 gate, WIRED (not a comment): on a synthetic -1% move the short must GAIN and shrink the
    move's |P&L|; gamma_resid must be < 0 BOTH directions. Catches a sign flip before any sizing."""
    L, p_a, p_b = pos["L_norm"], pos["p_a"], pos["p_b"]
    notional = eur_delta_usd(L, mid, p_a, p_b)
    for b in (-0.01, +0.01):
        midp = mid * (1 + b)
        dV = lp_value(L, midp, p_a, p_b) - lp_value(L, mid, p_a, p_b)
        short_pnl = notional * (mid - midp) / mid
        # gamma is <= 0 (the LP is short gamma). OUT OF RANGE (mid <= p_a, all EURC) V is LINEAR in mid so
        # gamma_resid is exactly 0 and float rounding lands it at a tiny POSITIVE epsilon — so this is a
        # tolerance check, not a strict <0 (a strict <0 spuriously faults the moment EUR drops below band).
        assert gamma_resid_usd(pos, mid) <= 1e-6, "G9 gamma_resid positive (convexity mislabel)"
        if b < 0:
            assert short_pnl > 0, "G9 SIGN ERROR: short loses on EUR-down"
            assert abs(dV + short_pnl) <= abs(dV), "G9 hedge did not reduce the move's |P&L|"


def hedge_open(state: dict, pos: dict, mid: float, now_ts: int) -> dict:
    """Size the paper short to the LP's live EUR $-delta at `mid` (residual = 0 at this instant). Charges
    one PERP_FEE on the opened notional — you cannot put on a ~$4,900 short for free. Runs the sign gate
    first, so a sign flip fails CLOSED before it can ever size a position."""
    hedge_sign_selftest(pos, mid)
    notional = eur_delta_usd(pos["L_norm"], mid, pos["p_a"], pos["p_b"])
    fee = PERP_FEE_BPS / 1e4 * abs(notional)
    return {"notional_usd": notional, "entry_mid": mid, "last_mid": mid, "last_ts": now_ts,
            "realized_usd": 0.0, "pnl_usd": 0.0, "carry_usd": 0.0, "fees_usd": fee,
            "n_rehedges": 0, "sized_at_ts": now_ts}


def hedge_step(state: dict, pos: dict, mid: float, br_state: str, now_ts: int,
               force_resize: bool) -> dict:
    """Mark the short on the LP's OWN mid (single feed), accrue carry, and re-size on the +/-band rule.
    Re-size ONLY when breaker == OK (D5); mark-only under FX_CLOSED / DISLOCATED. STALE never reaches here
    (the whole tick freezes upstream). On a resize, the unrealized chunk LOCKS into realized_usd and
    entry_mid resets, so pnl_usd is continuous across the move (no drop, no double-book)."""
    h = state["hedge"]
    book = state["book_usd"]
    # carry on the short notional, accrued per dt on every marking tick (exactly 0 at CARRY_RATE = 0)
    if h.get("last_ts"):
        dt_y = max(0.0, (now_ts - h["last_ts"]) / SEC_Y)
        h["carry_usd"] += CARRY_RATE * h["notional_usd"] * dt_y
    h["last_ts"], h["last_mid"] = now_ts, mid

    lp_delta = eur_delta_usd(pos["L_norm"], mid, pos["p_a"], pos["p_b"])
    residual = lp_delta - h["notional_usd"]               # dollar delta gap carried since last sizing
    # TRIGGER on the EUR MOVE since the last sizing, NOT on |residual|/book. The LP's gamma is huge
    # (~$1,619 of delta per 1% EUR move = 16% of book), so a |residual|/book>1% rule would fire every
    # ~0.06% move (~12,750 rehedges/yr) and the perp cost would dwarf the carry. delta_hedge.py defines
    # REHEDGE_BAND as a "+/-1% EUR move" trigger and calibrates REHEDGE_COST_1PCT to ~51 crossings/yr on
    # that basis (the PERP_FEE_BPS calibration above assumes it). delta_hedge_plan §02 phrases the trigger
    # as |residual/book|>0.01 — that is inconsistent with the engine + the cost model; the EUR-move band is
    # the validated, Sharpe-optimal design and is what keeps net APR ~flat. (Residual is still reported.)
    eur_move = (mid / h["entry_mid"] - 1.0) if h.get("entry_mid") else 0.0
    pnl_before = h["realized_usd"] + _hedge_unrealized(h, mid)
    resized = False
    if br_state == "OK" and (force_resize or abs(eur_move) > REHEDGE_BAND):
        h["realized_usd"] += _hedge_unrealized(h, mid)     # lock the current chunk
        h["entry_mid"] = mid                               # reset the entry -> unrealized now 0
        h["notional_usd"] = lp_delta                       # re-neutralize to the live delta
        h["fees_usd"] += PERP_FEE_BPS / 1e4 * abs(lp_delta)
        h["n_rehedges"] += 1
        h["sized_at_ts"] = now_ts
        resized = True
    h["pnl_usd"] = h["realized_usd"] + _hedge_unrealized(h, mid)   # total = realized + current unrealized
    return {"resized": resized, "residual_before": residual, "lp_delta": lp_delta, "eur_move": eur_move,
            "pnl_before": pnl_before, "pnl_after": h["pnl_usd"]}


def assert_conservation(state: dict, pos: dict, mid: float, marks: dict, h_snapshot: dict | None,
                        hres: dict | None, br_state: str) -> None:
    """8 conservation identities (G1-G8, delta_hedge_plan.html §05) + a sign/convexity self-test (G9, the
    §06 landmine-2 mitigation, wired as a gate). Raise on any breach so the caller REVERTS the overlay to
    last-good rather than persist a corrupted hedge. The naked book (validated engine) is recomputed
    independently here and must equal the marks (to 1e-9) regardless of the overlay."""
    h, book, TOL = state["hedge"], state["book_usd"], 1e-9
    # G1 nav_hedged - nav_naked == pnl + carry - fees, exactly (the ONLY delta between the two NAVs)
    assert abs((marks["nav_hedged"] - marks["nav"]) - (h["pnl_usd"] + h["carry_usd"] - h["fees_usd"])) < TOL, "G1"
    # G2 nav_naked / hodl / aave never reference the hedge (recompute from the validated formulas, compare)
    nav_indep = lp_value(pos["L_norm"], mid, pos["p_a"], pos["p_b"]) + pos["fees_usd"] - pos["gas_usd"]
    hodl_indep = hodl_value(state["inception"]["x0"], state["inception"]["y0"], mid)
    aave_indep = state["book_usd"] * (1 + AAVE_USDC_APR * marks["yrs"])
    assert (abs(marks["nav"] - nav_indep) < TOL and abs(marks["hodl"] - hodl_indep) < TOL
            and abs(marks["aave"] - aave_indep) < TOL), "G2"
    # G3 book principal never moves
    assert state["book_usd"] == BOOK_USD, "G3"
    # G4 single feed: the short marks on the SAME mid as V
    assert h["last_mid"] == mid, "G4"
    # G5 residual == 0 immediately after a (re)size
    if hres and hres.get("resized"):
        assert abs(hres["lp_delta"] - h["notional_usd"]) < 1e-6 * book, "G5"
        # G6 resize lock: pnl continuous across the move (no drop / no double-book)
        assert abs(hres["pnl_before"] - hres["pnl_after"]) < 1e-6, "G6"
    # G7 n_rehedges constant on a non-OK (mark-only) tick
    if br_state != "OK" and h_snapshot is not None:
        assert h["n_rehedges"] == h_snapshot["n_rehedges"], "G7"
    # G8 carry exactly 0 when CARRY_RATE == 0
    if CARRY_RATE == 0.0:
        assert h["carry_usd"] == 0.0, "G8"
    # G9 sign + convexity still hold at the live mid
    hedge_sign_selftest(pos, mid)


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
    nav = V + pos["fees_usd"] - pos["gas_usd"]                          # nav_naked — UNCHANGED by the overlay
    inc = state["inception"]
    hodl = hodl_value(inc["x0"], inc["y0"], mid)                       # IL peer: inception basket marked now
    yrs = (now_ts - state["inception_ts"]) / SEC_Y
    aave = state["book_usd"] * (1 + AAVE_USDC_APR * yrs)
    out = {"nav": nav, "V": V, "hodl": hodl, "aave": aave, "lvr_diag": pos["lvr_usd"],
           "fees": pos["fees_usd"], "gas": pos["gas_usd"], "yrs": yrs}
    # ---- delta-hedge overlay: nav_hedged is nav_naked + the three hedge lines (additive only). Present
    #      ONLY in a hedged book (state has a `hedge`); a naked book returns the original marks untouched. ----
    h = state.get("hedge")
    if h:
        contrib = h["pnl_usd"] + h["carry_usd"] - h["fees_usd"]        # the ONLY delta vs nav_naked
        out.update({"nav_hedged": nav + contrib,
                    "hedge_pnl": h["pnl_usd"], "hedge_carry": h["carry_usd"], "hedge_fees": h["fees_usd"],
                    "hedge_notional": h["notional_usd"], "hedge_n": h["n_rehedges"],
                    "eur_delta_usd": eur_delta_usd(pos["L_norm"], mid, pos["p_a"], pos["p_b"]),
                    "gamma_resid_usd": gamma_resid_usd(pos, mid)})
    return out


# ---- delta-hedge eval (onchain_fx_hedge_001) -------------------------------------------------
MIN_ANNUALIZE_YRS = 1.0 / 365.0   # don't annualize a sub-day window (matches live_render's headline rule):
#   a few minutes of noise annualizes to absurd 1000s-of-% Sharpe. Below this we keep sum_r2 (for the
#   SCALE-FREE vol ratio, which is honest short-window) but leave ann_vol/ann_ret/sharpe None.


def _ann_stats(rows: list, key: str, rf: float = AAVE_USDC_APR) -> dict | None:
    """Window return / vol / Sharpe of a NAV series, using the engine's OWN vol estimator (sum log-return^2
    / sum dt, F3-consistent). Annualized fields are None until the window is >= 1 day; sum_r2 is always
    returned so the naked-vs-hedged vol RATIO (annualization cancels) can be read even on a short window."""
    pts = [(r["ts"], r.get(key)) for r in rows if r.get(key) is not None]
    if len(pts) < 2:
        return None
    (t0, v0), (t1, vN) = pts[0], pts[-1]
    yrs = (t1 - t0) / SEC_Y
    if yrs <= 0 or v0 <= 0:
        return None
    sum_r2 = sum_dt = 0.0
    for (ta, va), (tb, vb) in zip(pts, pts[1:]):
        if va > 0 and vb > 0 and tb > ta:
            sum_r2 += math.log(vb / va) ** 2
            sum_dt += (tb - ta) / SEC_Y
    out = {"n": len(pts), "yrs": yrs, "end_usd": vN, "sum_r2": sum_r2,
           "ann_vol": None, "ann_ret": None, "sharpe": None}
    if yrs >= MIN_ANNUALIZE_YRS and sum_dt > 0:               # only annualize a meaningful window
        out["ann_vol"] = math.sqrt(sum_r2 / sum_dt)
        out["ann_ret"] = (vN / v0 - 1.0) / yrs
        out["sharpe"] = (out["ann_ret"] - rf) / out["ann_vol"] if out["ann_vol"] > 0 else None
    return out


def write_hedge_eval(state: dict) -> None:
    """Upsert the single onchain_fx_hedge_001 eval row: live naked-vs-hedged Sharpe/vol over the HEDGED
    window (rows carrying a nav_hedged). Question: does shorting the LP's EUR delta lift realized Sharpe
    FORWARD? Honest headline: net APR ~flat — a Sharpe trade, not a yield trade. Forward-only, single-vol."""
    h = state["hedge"]
    hedged_rows = [r for r in state["nav_hist"] if r.get("nav_hedged") is not None]
    naked, hedged = _ann_stats(hedged_rows, "nav"), _ann_stats(hedged_rows, "nav_hedged")
    # scale-free vol ratio (the annualization factor cancels) — the honest signal even on a sub-day window
    vol_ratio = (math.sqrt(hedged["sum_r2"] / naked["sum_r2"])
                 if (naked and hedged and naked.get("sum_r2", 0) > 0) else None)
    dvr = (1.0 - vol_ratio) if vol_ratio is not None else None     # directional vol removed (fraction)
    lift = (hedged["sharpe"] / naked["sharpe"]) if (naked and hedged and naked.get("sharpe")
            and hedged.get("sharpe") and naked["sharpe"] != 0) else None
    row = {
        "eval_id": "onchain_fx_hedge_001", "asof": state.get("last_tick_ts"),
        "question": "Does shorting the LP's live EUR delta lift realized Sharpe forward (net APR ~flat)?",
        "state": {"held_pool": state["position"]["pool"],
                  "naked_delta_frac": h["notional_usd"] / state["book_usd"],
                  "rehedge_band_pct": REHEDGE_BAND * 100, "carry_rate": CARRY_RATE,
                  "perp_fee_bps": PERP_FEE_BPS, "n_rehedges": h["n_rehedges"], "hedged_ticks": len(hedged_rows)},
        "action": "paper short EUR sized to eur_delta_usd(LP), re-sized on +/-1% band; carry booked at 0",
        "measured": {"naked": naked, "hedged": hedged, "sharpe_lift": lift,
                     "vol_ratio": vol_ratio, "directional_vol_removed_frac": dvr,
                     "annualized_ready": bool(naked and naked.get("sharpe") is not None),
                     "hedge_pnl_usd": h["pnl_usd"], "hedge_carry_usd": h["carry_usd"],
                     "hedge_cost_usd": h["fees_usd"]},
        "honest_headline": "Sharpe improver, not a yield improver: removes DELTA, never LVR (gamma "
                           "unhedgeable onchain). Single-window / single-vol-regime, forward-only.",
        "score_against": "realized forward naked-vs-hedged Sharpe + vol as the live run lengthens",
    }
    keep = []
    if HEDGE_EVAL.exists():
        for line in HEDGE_EVAL.read_text().splitlines():
            if line.strip() and json.loads(line).get("eval_id") != row["eval_id"]:
                keep.append(json.loads(line))
    keep.append(row)
    HEDGE_EVAL.write_text("\n".join(json.dumps(r, default=float) for r in keep) + "\n")


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

    # ---- delta-hedge overlay: size / mark / re-size (after recenter+scanner+realloc, before nav_marks) ----
    # Runs ONLY in a hedged book (ONCHAIN_FX_HEDGE=1). A naked book never enters here -> byte-identical output.
    # STALE never reaches here (frozen above). FX_CLOSED / DISLOCATED -> mark-only (no re-size). OK -> full.
    hres = h_snapshot = None
    if HEDGE_ON:
        state["hedge_enabled"] = True          # the renderer branches on this to show the hedge UI + tab
        state.setdefault("hedge", None)
    if HEDGE_ON and state["position"] is not None and br["state"] != "STALE":
        pos_h = state["position"]
        mid_h = feed["pools"][pos_h["pool"]]["mark_mid"]
        if state["hedge"] is None:
            # lazy open — covers BOTH true inception and a running-book MIGRATION (state.json with no `hedge`
            # key). Sizing the short IS a trade, so it is OK-GATED like a resize (D5): defer the open if the
            # first non-STALE tick is FX_CLOSED/DISLOCATED (don't anchor entry_mid off an untrusted-FV tick).
            # Wrapped fail-closed so a malformed open (e.g. the sign gate raising) can NEVER freeze the
            # validated naked book — it defers and flags instead.
            if br["state"] == "OK":
                try:
                    state["hedge"] = hedge_open(state, pos_h, mid_h, now_ts)
                    state.pop("hedge_open_fault", None)
                    actions.append({"type": "HEDGE_OPEN", "notional_usd": state["hedge"]["notional_usd"],
                                    "mid": mid_h, "frac_of_book": state["hedge"]["notional_usd"] / state["book_usd"]})
                except Exception as e:
                    state["hedge"] = None
                    state["hedge_open_fault"] = str(e)
                    actions.append({"type": "HEDGE_FAULT", "gate": f"open: {e}"})
                    print(f"  [HEDGE_OPEN deferred — {e}; naked book intact]")
        else:
            h_snapshot = dict(state["hedge"])              # last-good copy for fail-closed revert + G7
            # a recenter/realloc this tick moved the band -> the LP delta jumped -> force a re-size (D1)
            force = any(a["type"] in ("RECENTER", "REALLOCATE") for a in actions)
            hres = hedge_step(state, pos_h, mid_h, br["state"], now_ts, force)
            if hres["resized"]:
                actions.append({"type": "REHEDGE", "notional_usd": state["hedge"]["notional_usd"],
                                "mid": mid_h, "residual_before": hres["residual_before"],
                                "eur_move": hres["eur_move"], "forced": force,
                                "n_rehedges": state["hedge"]["n_rehedges"]})

    # ---- NAV marks (every non-failed tick that has a position) ----
    marks = None
    if state["position"] is not None and br["state"] != "STALE":
        pos = state["position"]
        mid = feed["pools"][pos["pool"]]["mark_mid"]
        marks = nav_marks(state, pos, mid, now_ts)
        # fail-closed conservation gates: a breach REVERTS the overlay to last-good (the naked book is the
        # validated engine and stays byte-identical), flags the fault, and never persists a corrupted hedge.
        if state.get("hedge") is not None:
            try:
                assert_conservation(state, pos, mid, marks, h_snapshot, hres, br["state"])
                state["hedge"].pop("fault", None)
            except Exception as e:   # broad on purpose: ANY gate failure (assert OR a compute error on a
                #                      corrupted state) must revert the overlay, never freeze the naked book.
                if h_snapshot is not None:
                    # step path: revert to last-good, re-mark its unrealized at the CURRENT mid (mark-only,
                    # no resize) so the persisted nav_hist row is internally consistent with its own mid.
                    state["hedge"] = h_snapshot
                    state["hedge"]["last_mid"] = mid
                    state["hedge"]["pnl_usd"] = state["hedge"]["realized_usd"] + _hedge_unrealized(state["hedge"], mid)
                    state["hedge"]["fault"] = str(e)
                else:
                    # fresh-open path: un-open and record a sticky open fault (retried next OK tick)
                    state["hedge"] = None
                    state["hedge_open_fault"] = str(e)
                marks = nav_marks(state, pos, mid, now_ts)   # re-mark on the reverted (good) hedge
                actions.append({"type": "HEDGE_FAULT", "gate": str(e)})
                print(f"  [HEDGE_FAULT {e} — overlay reverted; naked book intact]")
        row_h = {}
        if marks.get("hedge_notional") is not None:
            row_h = {"nav_hedged": marks["nav_hedged"], "hedge_pnl": marks["hedge_pnl"],
                     "hedge_carry": marks["hedge_carry"], "hedge_fees": marks["hedge_fees"],
                     "hedge_notional": marks["hedge_notional"], "eur_delta": marks["eur_delta_usd"],
                     "gamma_resid": marks["gamma_resid_usd"],
                     "L_norm": pos["L_norm"], "p_a": pos["p_a"], "p_b": pos["p_b"]}   # geometry for forward audit
        state["nav_hist"] = (state["nav_hist"] + [{
            "ts": now_ts, "nav": marks["nav"], "hodl": marks["hodl"], "aave": marks["aave"],
            "mid": mid, "breaker": br["state"], **row_h}])[-5000:]

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
    # ---- delta-hedge overlay fields (hedged book only; naked ledger stays byte-identical) ----
    if state.get("hedge") is not None:
        row.update({"nav_hedged": marks.get("nav_hedged") if marks else None,
                    "hedge_notional": marks.get("hedge_notional") if marks else None,
                    "hedge_pnl": marks.get("hedge_pnl") if marks else None,
                    "hedge_carry": marks.get("hedge_carry") if marks else None,
                    "hedge_fees": marks.get("hedge_fees") if marks else None,
                    "gamma_resid": marks.get("gamma_resid_usd") if marks else None,
                    "n_rehedges": state["hedge"]["n_rehedges"],
                    "hedge_fault": state["hedge"].get("fault")})
    append_ledger(row)

    # ---- delta-hedge eval row (onchain_fx_hedge_001): naked-vs-hedged Sharpe/vol, scored paper-live ----
    if state.get("hedge") is not None and marks is not None:
        write_hedge_eval(state)

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
        h = st.get("hedge")
        if h and m.get("nav_hedged") is not None:
            fault = f"  !! HEDGE_FAULT {h['fault']}" if h.get("fault") else ""
            print(f"   HEDGED ${m['nav_hedged']:,.2f} ({m['nav_hedged']-nav:+,.2f} vs naked)  · short ${h['notional_usd']:,.0f} "
                  f"({h['notional_usd']/st['book_usd']*100:.0f}% book)  pnl ${h['pnl_usd']:+.2f}  carry ${h['carry_usd']:+.2f}  "
                  f"cost ${h['fees_usd']:.2f}  rehedges {h['n_rehedges']}  γ-resid ${m.get('gamma_resid_usd',0):+.2f}{fault}")


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
