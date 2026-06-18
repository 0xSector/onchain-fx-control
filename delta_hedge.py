# @purpose delta_hedge.py — Quantify delta-hedging the onchain EUR/USDC yield LP by SHORTING EUR.
#          A concentrated v3 LP is born ~49% long EUR (it holds x EURC + y USDC; V = x*p + y, so its
#          $-delta to a 1% EUR move = x*p = the USD value of the EUR leg). book.py's net_apr =
#          gross_fee - LVR - basis_drag carries NO delta term (drift_2y is computed but never used),
#          so that ~half-book long-EUR exposure is an UNMODELED, UNCOMPENSATED directional bet whose
#          P&L (delta x FX move) dominates the live book's variance.
#
#          THIS ENGINE (read-only, reproducible, paper):
#            1. Reads the LIVE paper book (artifacts/book/live/state.json) and decomposes its realized
#               P&L into DELTA (frozen-basket directional) / GAMMA-LVR / FEES.
#            2. Computes the LP's EUR delta PROFILE across the +/-3% range (0% at +3% = all USDC, ~98%
#               at -3% = all EURC), from the exact Uniswap-v3 amount math (self-tested).
#            3. Models NAKED vs DELTA-HEDGED book: net APR, P&L vol, Sharpe — across a carry-scenario
#               grid (Ostium pays the short the USD-EUR rate gap; Avantis/Gains/Aave do not) and a
#               rehedge band. Vol inputs sourced to the 2026-06-18 multi-agent validation.
#
#          HONEST HEADLINE (do not bury): the hedge is a SHARPE improver (~1.2 -> ~2.7-3.3), NOT a
#          yield improver (net APR ~flat, slightly negative at today's narrowed carry). It removes the
#          directional FX noise; it does NOT touch LVR (gamma is unhedgeable onchain — no EUR options
#          venue exists as of 2026-06). Book carry at 0; treat any positive carry as upside, re-underwrite
#          each snapshot. Numbers are single-week / single-vol-regime point estimates.

from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ART = ROOT / "artifacts"
LIVE = ART / "book" / "live" / "state.json"
OUT = ART / "book" / "delta_hedge.json"

# ---- validated inputs (sourced, not invented) -----------------------------------------------
NET_APR_LP = 0.0898      # EUR/USDC leg net fee carry (positions.json; gross 13.30 - LVR 4.24 - basis 0.077)
GROSS_APR = 0.1330       # measured gross fee APR
LVR_APR = 0.0424         # E*sigma^2/8 at +/-3%, sigma=7.11% (irreducible — gamma, NOT hedgeable onchain)
RF = 0.045               # risk-free / do-nothing baseline (Aave USDC)
RESID_NOISE = 0.015      # fee+LVR realization vol (the least-grounded input; the hedged-vol floor)
REHEDGE_COST_1PCT = 0.0031   # rehedge cost on book at a +/-1% band (~51 crossings/yr x ~$0.62 Ostium resize)

# carry on the SHORT NOTIONAL, annualized. Sign is the headline finding of the venue research.
# Ostium rollover = real futures term structure (two-sided) -> short EUR COLLECTS the USD-EUR gap.
# Avantis/Gains vault borrow-fee -> both sides pay, minority pays less, NO rate-differential income.
# Aave EURC-borrow -> USDC_supply - EURC_borrow brackets zero.
CARRY_SCENARIOS = [
    ("Ostium, 160bp gap (fair book, optimistic)", +0.0175, "Arbitrum RWA perp; pre-Jun-2026 rate gap"),
    ("Ostium, ~125bp gap (Jun-2026, narrowed)",   +0.0105, "ECB hiked to 2.25% Jun-2026; gap compressing"),
    ("carry booked at 0 (RECOMMENDED default)",    0.0,     "conservative; treat any + carry as upside"),
    ("Aave EURC-borrow (~flat)",                  -0.0020, "USDC supply ~3.15% - EURC borrow ~4.05% ~ 0; shorts the real EURC token"),
    ("Avantis / Gains vault fee (carry-negative)", -0.0100, "same-chain (Base) but pays a margin fee, no rate carry"),
]

REHEDGE_BAND = 0.01      # +/-1% EUR move rehedge trigger (Sharpe-optimal per the validation model)


# ---- Uniswap-v3 concentrated-liquidity amount math (token0=EURC, token1=USDC, p = USDC/EURC) ---
def v3_amounts(L: float, p: float, pa: float, pb: float) -> tuple[float, float]:
    """EURC (x) and USDC (y) held by a liquidity-L position over [pa, pb] at price p."""
    sp, spa, spb = math.sqrt(p), math.sqrt(pa), math.sqrt(pb)
    if p <= pa:                          # all EURC
        x = L * (1.0 / spa - 1.0 / spb); y = 0.0
    elif p >= pb:                        # all USDC
        x = 0.0; y = L * (spb - spa)
    else:
        x = L * (1.0 / sp - 1.0 / spb); y = L * (sp - spa)
    return x, y


def eur_delta_usd(L: float, p: float, pa: float, pb: float) -> float:
    """USD value of the EUR (EURC) leg = the $-exposure that moves 1:1 with EUR/USD."""
    x, _ = v3_amounts(L, p, pa, pb)
    return x * p


def selftest(state: dict, tol: float = 0.01) -> bool:
    """The v3 amount math must recover the live book's stored entry x0,y0 from (L, entry_mid, pa, pb)."""
    pos = state["position"]
    L, p, pa, pb = pos["L_norm"], pos["entry_mid"], pos["p_a"], pos["p_b"]
    x, y = v3_amounts(L, p, pa, pb)
    x0, y0 = pos["x0"], pos["y0"]
    rel = max(abs(x - x0) / x0, abs(y - y0) / y0)
    ok = rel < tol
    print(f"[selftest] v3 amounts from L={L:.0f}: x={x:.1f} (stored {x0:.1f}), y={y:.1f} (stored {y0:.1f})  "
          f"rel_err={rel*100:.2f}%  -> {'PASS' if ok else 'FAIL'}")
    assert ok, f"v3 amount math off by {rel*100:.1f}% (> {tol*100:.0f}% tol)"
    return ok


# ---- live P&L decomposition -------------------------------------------------------------------
def decompose_live(state: dict) -> dict:
    pos, hist = state["position"], state["nav_hist"]
    book0 = state["book_usd"]
    fees = pos["fees_usd"]
    nav, hodl, m0, m1 = hist[-1]["nav"], hist[-1]["hodl"], hist[0]["mid"], hist[-1]["mid"]
    total = nav - book0
    delta_pnl = hodl - book0                  # frozen inception basket marked at live mid = pure directional
    gamma_pnl = nav - hodl - fees             # residual = LVR/gamma/rebalance (absorbs model error)
    eur_move = m1 / m0 - 1.0
    return {
        "window_ticks": len(hist), "eur_move_pct": eur_move * 100,
        "total_pnl": total, "delta_pnl": delta_pnl, "fee_pnl": fees, "gamma_lvr_pnl": gamma_pnl,
        "pct_of_loss_from_delta": (delta_pnl / total * 100) if total else float("nan"),
        "true_lp_carry_window": fees + gamma_pnl,   # what a delta-hedged book keeps
    }


# ---- delta profile + naked/hedged model -------------------------------------------------------
def delta_profile(state: dict) -> dict:
    pos = state["position"]
    L, pa, pb = pos["L_norm"], pos["p_a"], pos["p_b"]
    p0 = state["nav_hist"][-1]["mid"]
    book = state["book_usd"]
    d_lo = eur_delta_usd(L, pb, pa, pb)       # at +3% (EUR expensive): all USDC -> ~0 EUR delta
    d_mid = eur_delta_usd(L, p0, pa, pb)
    d_hi = eur_delta_usd(L, pa, pa, pb)       # at -3% (EUR cheap): all EURC -> ~full-book EUR delta
    # residual delta left by a static short sized at p0, over a +/-band move (the rehedge band)
    band = REHEDGE_BAND
    samples = [p0 * (1 + band * t / 10) for t in range(-10, 11)]
    resid = [eur_delta_usd(L, p, pa, pb) - d_mid for p in samples]
    rms_resid = math.sqrt(sum(r * r for r in resid) / len(resid))
    return {
        "mid": p0, "p_a": pa, "p_b": pb,
        "delta_at_plus3pct_usd": d_lo, "delta_at_mid_usd": d_mid, "delta_at_minus3pct_usd": d_hi,
        "delta_at_mid_frac": d_mid / book,
        "rms_residual_within_band_usd": rms_resid, "rehedge_band_pct": band * 100,
    }


def model(state: dict, prof: dict) -> dict:
    book = state["book_usd"]
    sigma = state["sigma"]["prior"]
    avg_delta_frac = prof["delta_at_mid_frac"]           # ~time-average ~ mid weight
    resid_delta_frac = prof["rms_residual_within_band_usd"] / book

    naked_vol = math.hypot(avg_delta_frac * sigma, RESID_NOISE)
    hedged_vol = math.hypot(resid_delta_frac * sigma, RESID_NOISE)

    naked = {"net_apr": NET_APR_LP, "pnl_vol": naked_vol,
             "sharpe": (NET_APR_LP - RF) / naked_vol,
             "directional_vol_removed": avg_delta_frac * sigma}

    rows = []
    for label, carry_on_notional, note in CARRY_SCENARIOS:
        carry_on_book = carry_on_notional * avg_delta_frac
        net = NET_APR_LP + carry_on_book - REHEDGE_COST_1PCT
        rows.append({"scenario": label, "note": note,
                     "carry_on_notional": carry_on_notional, "carry_on_book": carry_on_book,
                     "rehedge_cost": REHEDGE_COST_1PCT, "net_apr": net,
                     "pnl_vol": hedged_vol, "sharpe": (net - RF) / hedged_vol})
    return {"sigma": sigma, "avg_delta_frac": avg_delta_frac, "resid_delta_frac": resid_delta_frac,
            "naked": naked, "hedged": rows,
            "sharpe_lift_vs_naked": rows[2]["sharpe"] / naked["sharpe"]}   # vs the carry=0 default


def main() -> None:
    state = json.loads(LIVE.read_text())
    print("\n=== DELTA-HEDGE ENGINE — onchain EUR/USDC yield LP (paper, $10k) ===\n")
    selftest(state)

    decomp = decompose_live(state)
    prof = delta_profile(state)
    mdl = model(state, prof)

    print(f"\n--- LIVE P&L decomposition ({decomp['window_ticks']} ticks, EUR {decomp['eur_move_pct']:+.2f}%) ---")
    print(f"  total   ${decomp['total_pnl']:+7.2f}")
    print(f"  DELTA   ${decomp['delta_pnl']:+7.2f}   ({decomp['pct_of_loss_from_delta']:.0f}% of the move)  <- long-EUR, uncompensated, UNMODELED")
    print(f"  fees    ${decomp['fee_pnl']:+7.2f}")
    print(f"  gamma   ${decomp['gamma_lvr_pnl']:+7.2f}   (LVR/rebalance residual)")
    print(f"  true LP carry kept by a hedged book: ${decomp['true_lp_carry_window']:+.2f}")

    print(f"\n--- EUR delta profile across the +/-3% range ---")
    print(f"  at +3% (EUR rich): ${prof['delta_at_plus3pct_usd']:7.0f}  (all USDC)")
    print(f"  at mid           : ${prof['delta_at_mid_usd']:7.0f}  ({prof['delta_at_mid_frac']*100:.0f}% of book long EUR)")
    print(f"  at -3% (EUR cheap): ${prof['delta_at_minus3pct_usd']:7.0f}  (all EURC)")
    print(f"  rms residual within +/-{prof['rehedge_band_pct']:.0f}% band: ${prof['rms_residual_within_band_usd']:.0f}")

    n = mdl["naked"]
    print(f"\n--- NAKED vs DELTA-HEDGED (rf={RF*100:.1f}%, sigma={mdl['sigma']*100:.2f}%) ---")
    print(f"  NAKED   net {n['net_apr']*100:5.2f}%  vol {n['pnl_vol']*100:4.2f}%  Sharpe {n['sharpe']:.2f}   "
          f"(removes {n['directional_vol_removed']*100:.2f}pts of directional vol)")
    for r in mdl["hedged"]:
        print(f"  HEDGED  net {r['net_apr']*100:5.2f}%  vol {r['pnl_vol']*100:4.2f}%  Sharpe {r['sharpe']:.2f}   {r['scenario']}")
    print(f"\n  Sharpe lift (carry=0 default vs naked): {mdl['sharpe_lift_vs_naked']:.2f}x")
    print(f"  HONEST: Sharpe improves; net APR ~flat. Hedge removes DELTA, not LVR (gamma unhedgeable onchain).")

    OUT.write_text(json.dumps({"decomp": decomp, "profile": prof, "model": mdl,
                               "inputs": {"net_apr_lp": NET_APR_LP, "lvr_apr": LVR_APR, "rf": RF,
                                          "resid_noise": RESID_NOISE, "rehedge_cost_1pct": REHEDGE_COST_1PCT}},
                              indent=2, default=float))
    print(f"\nartifact -> {OUT}")


if __name__ == "__main__":
    main()
