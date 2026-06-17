# @purpose live_econ.py - self-contained concentrated-liquidity economics for the LIVE forward-press LP.
#          These primitives are copied VERBATIM from the validated book.py (cap_efficiency, lvr_apr) and
#          sim.py (Elsts lp geometry), which were adversarially reviewed 2026-06-15. They are vendored here
#          ONLY so the live tool can run in the PUBLIC onchain-fx-control repo without importing the private
#          analysis pipeline (book.py/sim.py pull pandas + the Allium query chain). This is textbook DeFi
#          math — Uniswap-v3 capital efficiency, Milionis-Moallemi-Roughgarden LVR, Elsts closed forms — not
#          proprietary methodology, so it is safe to publish. book.py/sim.py remain the single source of
#          truth; if those change, re-vendor here (the values are simple closed forms, no drift risk).
#
#          Pure stdlib (math only) — NO pandas/numpy — so the GitHub Actions cron needs zero pip installs.
#          Read-only math. Paper tool — no capital, no on-chain actions.

from __future__ import annotations

import math

# ---- constants (book.py / sim.py) ------------------------------------------------------------
BOOK_USD = 10_000.0                 # paper book size
REF_RANGE = 0.03                    # +/-3% reference LP band (headline LVR decomposition)
WEEKS_Y = 52.0
SEC_Y = 365.0 * 24 * 3600.0         # seconds per year (calendar)
AAVE_USDC_APR = 0.045               # do-nothing opportunity-cost baseline (USDC supply)
MIN_TVL = 300_000.0                 # investability floor: pool deep enough that fees/TVL is a real APR
MIN_VOL_WK = 1_000_000.0            # genuine two-sided organic flow, not a handful of fills


# ---- LVR / capital efficiency (book.py) ------------------------------------------------------
def cap_efficiency(half_range: float) -> float:
    """Uniswap-v3 capital-efficiency factor E for a symmetric band +/-half_range vs full range.
    E = 1 / (1 - (p_a/p_b)^(1/4)). +/-2%~100x, +/-3%~67x, +/-5%~40x, +/-10%~20x."""
    pa, pb = 1.0 - half_range, 1.0 + half_range
    return 1.0 / (1.0 - (pa / pb) ** 0.25)


def lvr_apr(ann_vol: float, half_range: float) -> float:
    """Annualized LVR as a fraction of position value: E(range) * sigma^2 / 8."""
    return cap_efficiency(half_range) * ann_vol ** 2 / 8.0


# ---- concentrated-liquidity primitives (sim.py; Elsts closed forms) --------------------------
def lp_amounts(L: float, p: float, p_a: float, p_b: float) -> tuple[float, float]:
    """Token amounts for a v3 position of liquidity L at price p (USD/local) in [p_a,p_b].
    x = local units, y = USD units. Out-of-range -> single asset (Elsts)."""
    sp, spa, spb = math.sqrt(p), math.sqrt(p_a), math.sqrt(p_b)
    if p <= p_a:                       # all local
        return L * (1.0 / spa - 1.0 / spb), 0.0
    if p >= p_b:                       # all USD
        return 0.0, L * (spb - spa)
    x = L * (spb - sp) / (sp * spb)
    y = L * (sp - spa)
    return x, y


def lp_value(L: float, p: float, p_a: float, p_b: float) -> float:
    """Position value in USD: x*p + y."""
    x, y = lp_amounts(L, p, p_a, p_b)
    return x * p + y


def L_for_deposit(usd: float, p: float, p_a: float, p_b: float) -> float:
    """Liquidity L such that the position is worth `usd` at price p in [p_a,p_b]."""
    v1 = lp_value(1.0, p, p_a, p_b)
    return usd / v1 if v1 > 0 else 0.0


def hodl_value(x0: float, y0: float, p: float) -> float:
    """Value of just holding the entry token amounts (the IL benchmark = LVR's rebalancing peer)."""
    return x0 * p + y0


# ---- self-check: vendored values must match the validated source --------------------------------
if __name__ == "__main__":
    # parity check vs book.py/sim.py when run inside the private repo (skipped silently if absent)
    try:
        import book, sim
        assert abs(cap_efficiency(0.03) - book.cap_efficiency(0.03)) < 1e-12
        assert abs(lvr_apr(0.071, 0.03) - book.lvr_apr(0.071, 0.03)) < 1e-12
        assert abs(lp_value(1e5, 1.159, 1.124, 1.194) - sim.lp_value(1e5, 1.159, 1.124, 1.194)) < 1e-9
        for c in ("BOOK_USD", "REF_RANGE", "WEEKS_Y", "MIN_TVL", "MIN_VOL_WK"):
            assert getattr(book, c) == globals()[c], c
        for c in ("SEC_Y", "AAVE_USDC_APR"):
            assert getattr(sim, c) == globals()[c], c
        print("live_econ parity vs book.py/sim.py: OK")
    except ImportError:
        print("book/sim not importable here (public repo) — parity check skipped")
