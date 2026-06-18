# @purpose live_mid.py - live_sim_plan.html S0: the live onchain feed for the forward-press LP tool.
#          Reads the EUR/USDC Base pools DIRECTLY from a public Base RPC (eth_call, no key, no Allium
#          budget) so the live tick is cloud-runnable with zero secrets. Replaces tick_data.py's Allium
#          Swap-log pull (minutes of ingestion lag, query budget) for the per-tick loop; Allium stays
#          the weekly validation/backfill layer.
#
#          TWO pool kinds, dispatched by POOLS[label]["kind"]:
#            v3  (Uniswap-v3-family: Aerodrome/Pancake/Alien/Uniswap) - one CONTRACT per pool; reads
#                slot0()/liquidity()/observe() on the pool address; Swap logs on the pool address.
#            v4  (Uniswap v4) - a SINGLETON PoolManager; pools are keyed by poolId (no per-pool address,
#                no slot0/observe). mid+L read via the Base StateView getSlot0/getLiquidity(poolId); Swap
#                logs read on the PoolManager filtered by topic1=poolId. v4 has no built-in oracle, so the
#                mark falls back to SPOT (flagged mark_is_spot) - acceptable because all v4 pools here are
#                WATCH-ONLY (screened ineligible in live_lp, never hold the paper book).
#
#          Per pool, per tick, this exposes (verified live 2026-06-17 v3 / 2026-06-18 v4 on Base RPC):
#            - MARK mid  : v3 observe([TWAP_S,0]) 5-min TWAP (F1); v4 spot (no oracle -> mark_is_spot)
#            - SPOT mid  : v3 slot0() / v4 StateView.getSlot0(poolId) sqrtPriceX96  (breaker spot-vs-Pyth)
#            - L_active  : v3 liquidity() / v4 StateView.getLiquidity(poolId)        (F2 L-unit fee share)
#            - volume    : eth_getLogs(Swap) from last_block (v3 on pool / v4 on PoolManager+poolId, F5)
#            - gas, block, block_ts
#
#          mid = (sqrtPriceX96 / 2^96)^2 * 10^(dec0-dec1). All pools are EUR-stable(6)/USD-stable(6),
#          token0=EUR, equal decimals -> mid = (sqrtP/2^96)^2 = USD per EUR (matches the tape `p` convention).
#          TWAP price = 1.0001^avgTick where avgTick = (tickCum[1]-tickCum[0]) / TWAP_S (token1/token0).
#
#          Read-only public market data. No auth, no wallet, no on-chain actions. Paper tool.

from __future__ import annotations

import json
import math
import time
import urllib.request

# Public Base RPCs (no key). mainnet.base.org 403s without a UA header; all verified working with one.
# First is primary; the rest are fallbacks tried in order on any failure (review F11 robustness).
RPCS = [
    "https://mainnet.base.org",
    "https://base-rpc.publicnode.com",
    "https://base.drpc.org",
    "https://1rpc.io/base",
]
UA = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (onchain-fx-research; read-only)"}

SWAP_TOPIC0 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"  # v3 Swap
Q96 = 2 ** 96
TWAP_S = 300                       # 5-min TWAP window for the robust mark (review F1)
GETLOGS_CHUNK = 5_000              # max block span per eth_getLogs (public-RPC friendly); chunked if gap larger

# ---- Uniswap v4 (singleton) constants (Base) ------------------------------------------------
# v4 has no per-pool contract: every pool lives in one PoolManager and is keyed by poolId. mid+L are read
# from the StateView lens contract; volume from PoolManager Swap logs filtered by topic1=poolId.
V4_POOL_MANAGER = "0x498581ff718922c3f8e6a244956af099b2652b2b"   # Base v4 PoolManager
V4_STATEVIEW    = "0xa3c0c9b65bad0b08107aa264b0f3db444b867a71"   # Base v4 StateView (verified 2026-06-18)
V4_SWAP_TOPIC0  = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"  # v4 Swap event
SEL_GETSLOT0    = "0xc815641c"     # StateView.getSlot0(bytes32) -> (sqrtPriceX96, tick, protoFee, lpFee)
SEL_GETLIQUIDITY = "0xfa6793d5"    # StateView.getLiquidity(bytes32) -> uint128

# label -> {kind, addr|pool_id, fee_bps, dec0, dec1}. All token0=EUR-stable(6) / token1=USD-stable(6), so
# mid = (sqrtPriceX96/2^96)^2 = USD per EUR (10^(dec0-dec1)=1). v3 verified live 2026-06-17 via token0/
# token1/fee/decimals introspection; v4 confirmed 2026-06-18 (probe_v4_pools.py: deterministic poolId =
# keccak(abi.encode(PoolKey)) + StateView read + mid reconciled to v3 within 0.1bp). The 3 Aerodrome tiers
# are the deep, screen-passing set (S2 clean mids); the rest are THIN Base EUR pools for visibility only
# (all <$300k TVL -> watch-not-allocate in live_lp). Tessera / Curve / Balancer / Aerodrome-v2 EUR pools
# are NOT here (no readable mid). Only the ONE active no-hook v4 EUR pool is added; the other no-hook v4
# combos are initialized-but-dead (L=0/$0), and hook-bearing v4 EUR pools aren't token-confirmable yet.
POOLS = {
    "aero_e846":    {"kind": "v3", "addr": "0xe846373c1a92b167b4e9cd5d8e4d6b1db9e90ec7", "fee_bps": 5,  "dec0": 6, "dec1": 6},  # deepest TVL ($2.87M), canonical mid
    "aero_f39b":    {"kind": "v3", "addr": "0xf39b7c34be147f5dc1bc374f27af2e9f03ad3113", "fee_bps": 1,  "dec0": 6, "dec1": 6},  # 1bp tier, highest volume
    "aero_c5e5":    {"kind": "v3", "addr": "0xc5e51044eb7318950b1afb044fccfb25782c48c1", "fee_bps": 30, "dec0": 6, "dec1": 6},  # 30bp tier ($219k TVL)
    "pancake_1ca4": {"kind": "v3", "addr": "0x1ca42c7219f0cb1b67927e26502320cb98f725bd", "fee_bps": 1,  "dec0": 6, "dec1": 6},  # PancakeSwap v3, EURC/USDC, $190k
    "aero_183c":    {"kind": "v3", "addr": "0x183cefd0928ea4d54c9d726dd975fab561705c86", "fee_bps": 5,  "dec0": 6, "dec1": 6},  # Aerodrome, EURAU/USDC (diff EUR issuer)
    "alien_7b2c":   {"kind": "v3", "addr": "0x7b2c99188d8ec7b82d6b3b3b1c1002095f1b8498", "fee_bps": 1,  "dec0": 6, "dec1": 6},  # AlienBase v3, EURC/USDC, $112k
    "uni_7279":     {"kind": "v3", "addr": "0x7279c08a36333e12c3fc81747963264c100d66fb", "fee_bps": 5,  "dec0": 6, "dec1": 6},  # Uniswap v3, EURC/USDC, $94k
    "pancake_f0c5": {"kind": "v3", "addr": "0xf0c559af52bce48b3f3710604a59b4feaefd5555", "fee_bps": 1,  "dec0": 6, "dec1": 6},  # PancakeSwap v3, EURC/USDT (diff USD stable)
    "uni_03d8":     {"kind": "v3", "addr": "0x03d8219070e54a55a9ce60889ead2ffd18eb6aa9", "fee_bps": 30, "dec0": 6, "dec1": 6},  # Uniswap v3, EURC/USDC, $12k (dust)
    # Uniswap v4 (singleton, poolId-keyed). EURC/USDC 5bp/tickSpacing-10, no hook. ~$7k/28h, mid==v3.
    "univ4_64db":   {"kind": "v4", "pool_id": "0x64db64264317af2fa1cc5a5deff9900dcf504d426580ab16dc59279b4ade4423", "fee_bps": 5, "dec0": 6, "dec1": 6},  # Uniswap v4, EURC/USDC (watch-only)
}

# v3 function selectors (called on the pool contract address)
SEL_SLOT0 = "0x3850c7bd"           # slot0()
SEL_LIQUIDITY = "0x1a686502"       # liquidity()
SEL_OBSERVE = "0x883bdbfd"         # observe(uint32[])


class RPCError(RuntimeError):
    pass


# ---- low-level JSON-RPC with multi-endpoint failover -----------------------------------------
def _rpc(method: str, params: list, _rpcs: list | None = None) -> dict:
    """Single JSON-RPC call, failing over across RPCS. Raises RPCError if every endpoint fails."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = None
    for url in (_rpcs or RPCS):
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, data=body, headers=UA)
                with urllib.request.urlopen(req, timeout=20) as r:
                    d = json.load(r)
                if "error" in d:
                    last = f"{url}: {d['error']}"
                    break                       # an RPC-level error won't fix on retry; try next endpoint
                return d
            except Exception as e:              # network / 403 / timeout -> brief backoff then next
                last = f"{url}: {e!r}"
                time.sleep(0.4 * (attempt + 1))
    raise RPCError(f"all RPCs failed for {method}: {last}")


def _call(to: str, data: str) -> str:
    r = _rpc("eth_call", [{"to": to, "data": data}, "latest"])
    res = r.get("result")
    if not res or res == "0x":
        raise RPCError(f"empty eth_call result for {to} {data[:10]}")
    return res


def _words(hexstr: str) -> list[str]:
    b = hexstr[2:] if hexstr.startswith("0x") else hexstr
    return [b[i:i + 64] for i in range(0, len(b), 64)]


def _u(word: str) -> int:
    return int(word, 16)


def _s(word: str) -> int:
    """signed two's-complement of a 256-bit word (int24 tick / int56 tickCum / int256 amount)."""
    v = int(word, 16)
    return v - (1 << 256) if v >= (1 << 255) else v


# ---- pool reads ------------------------------------------------------------------------------
def block_now() -> dict:
    """Latest block number + unix timestamp (the 'as of' clock for this tick's mid)."""
    blk = _rpc("eth_getBlockByNumber", ["latest", False])["result"]
    return {"number": int(blk["number"], 16), "ts": int(blk["timestamp"], 16)}


def gas_price_gwei() -> float:
    return int(_rpc("eth_gasPrice", [])["result"], 16) / 1e9


def spot_mid(label: str) -> float:
    """Spot mid (USD/EUR) from sqrtPriceX96. BREAKER USE ONLY - never the position mark (F1).
    v3: slot0() on the pool. v4: StateView.getSlot0(poolId) word[0]."""
    p = POOLS[label]
    if p["kind"] == "v4":
        sqrtP = _u(_words(_call(V4_STATEVIEW, SEL_GETSLOT0 + p["pool_id"][2:]))[0])
    else:
        sqrtP = _u(_words(_call(p["addr"], SEL_SLOT0))[0])
    return (sqrtP / Q96) ** 2 * 10 ** (p["dec0"] - p["dec1"])


def liquidity_active(label: str) -> int:
    """In-range L (raw pool units). Denominator of the F2 L-unit fee share.
    v3: liquidity() on the pool. v4: StateView.getLiquidity(poolId)."""
    p = POOLS[label]
    if p["kind"] == "v4":
        return _u(_words(_call(V4_STATEVIEW, SEL_GETLIQUIDITY + p["pool_id"][2:]))[0])
    return _u(_words(_call(p["addr"], SEL_LIQUIDITY))[0])


def twap_mid(label: str, window_s: int = TWAP_S) -> float:
    """observe([window,0]) -> time-weighted avg mid over the last `window_s` (the ROBUST mark, F1).
    Falls back to spot only if the pool has no oracle cardinality (raises differently).
    v4 has NO observe() (singleton, no per-pool oracle) -> raise so poll_pool marks on spot (mark_is_spot)."""
    p = POOLS[label]
    if p["kind"] == "v4":
        raise RPCError("v4 singleton has no observe() oracle; mark on spot")
    addr, d0, d1 = p["addr"], p["dec0"], p["dec1"]
    data = (SEL_OBSERVE
            + "0000000000000000000000000000000000000000000000000000000000000020"   # offset to array
            + format(2, "064x")                                                    # array length = 2
            + format(int(window_s), "064x") + format(0, "064x"))                   # secondsAgos = [window, 0]
    words = _words(_call(addr, data))
    off1 = _u(words[0]) // 32                 # offset (bytes) of tickCumulatives[] -> word index
    n = _u(words[off1])                       # array length
    tc = [_s(words[off1 + 1 + k]) for k in range(n)]
    avg_tick = (tc[1] - tc[0]) / float(window_s)            # ticks are token1/token0 = USDC/EURC
    return (1.0001 ** avg_tick) * 10 ** (d0 - d1)


def swap_volume_since(label: str, from_block: int, to_block: int) -> dict:
    """Sum USD volume of Swap events in (from_block, to_block], chunked for public-RPC span limits.
    USD per swap = |amount1| / 10^dec1 (token1 = USD stable, amount1 is data word[1] in BOTH v3 and v4).
    Returns {volume_usd, n_swaps, to_block}.

    v3: query the pool address, topic [v3 Swap]. v4: query the PoolManager, topic [v4 Swap, poolId] so
    only this pool's swaps come back (the singleton firehose is filtered server-side by topic1).

    Self-healing (F5): the caller persists `to_block` as the next `from_block`, so a delayed or missed
    tick just widens the window instead of dropping or double-counting volume."""
    p = POOLS[label]
    d1 = p["dec1"]
    if to_block <= from_block:
        return {"volume_usd": 0.0, "n_swaps": 0, "to_block": to_block}
    if p["kind"] == "v4":
        log_addr, topics = V4_POOL_MANAGER, [V4_SWAP_TOPIC0, p["pool_id"]]
    else:
        log_addr, topics = p["addr"], [SWAP_TOPIC0]
    vol, n = 0.0, 0
    lo = from_block + 1
    while lo <= to_block:
        hi = min(lo + GETLOGS_CHUNK - 1, to_block)
        logs = _rpc("eth_getLogs", [{"address": log_addr, "topics": topics,
                                     "fromBlock": hex(lo), "toBlock": hex(hi)}]).get("result", [])
        for lg in logs:
            w = _words(lg["data"])
            if len(w) < 5:
                continue
            amount1 = _s(w[1])                 # int128/int256 USD raw, signed (word[1] in v3 and v4)
            vol += abs(amount1) / 10 ** d1
            n += 1
        lo = hi + 1
    return {"volume_usd": vol, "n_swaps": n, "to_block": to_block}


# ---- the tick-level poll --------------------------------------------------------------------
def poll_pool(label: str, from_block: int | None, to_block: int) -> dict:
    """One pool's full observation for a tick. from_block=None on first sight -> volume window starts now
    (no retro-volume). Any individual read failing is surfaced as ok=False so the breaker can FREEZE."""
    p = POOLS[label]
    obs = {"label": label, "kind": p["kind"], "addr": p.get("addr") or p.get("pool_id"),
           "fee_bps": p["fee_bps"], "ok": True, "errors": []}
    try:
        obs["mark_mid"] = twap_mid(label)
    except Exception as e:
        # no observe() oracle (some thin pools lack one) -> mark on spot. NOT a failure: ok stays True,
        # flagged mark_is_spot. Only a missing spot too is a real failure (set below).
        obs["errors"].append(f"twap:{e}")
        try:
            obs["mark_mid"] = spot_mid(label)
            obs["mark_is_spot"] = True
        except Exception as e2:
            obs["ok"] = False
            obs["errors"].append(f"spot_fallback:{e2}")
    try:
        obs["spot_mid"] = spot_mid(label)
    except Exception as e:
        obs["ok"] = False
        obs["errors"].append(f"spot:{e}")
    try:
        obs["L_active"] = liquidity_active(label)
    except Exception as e:
        obs["ok"] = False
        obs["errors"].append(f"liquidity:{e}")
    try:
        vol = swap_volume_since(label, from_block if from_block is not None else to_block, to_block)
        obs.update({"interval_vol_usd": vol["volume_usd"], "n_swaps": vol["n_swaps"],
                    "from_block": from_block, "to_block": to_block,
                    "first_sight": from_block is None})
    except Exception as e:
        obs["ok"] = False
        obs["errors"].append(f"getLogs:{e}")
        obs.update({"interval_vol_usd": 0.0, "n_swaps": 0, "to_block": to_block, "first_sight": from_block is None})
    return obs


def poll(labels: list[str], cursors: dict | None = None) -> dict:
    """Poll every candidate pool for this tick. cursors: {label: last_block}. Returns a feed dict the
    tick consumes: per-pool obs + the shared block clock + gas. Network-bound but typically ~1-3s."""
    cursors = cursors or {}
    blk = block_now()
    try:
        gas = gas_price_gwei()
    except Exception:
        gas = float("nan")
    pools = {lbl: poll_pool(lbl, cursors.get(lbl), blk["number"]) for lbl in labels}
    return {"block": blk["number"], "block_ts": blk["ts"], "gas_gwei": gas, "pools": pools}


if __name__ == "__main__":
    # smoke test: prove the whole live-mid path with zero auth and reconcile TWAP vs spot vs Pyth.
    import pyth
    f = poll(list(POOLS))
    print(f"block {f['block']} @ {f['block_ts']}  gas {f['gas_gwei']:.4f} gwei")
    fvn = pyth.fv_now("eur/usd", f["block_ts"])
    print(f"Pyth FX.EUR/USD fv_now = {fvn['fv']} ({fvn['reason']})")
    for lbl, o in f["pools"].items():
        if not o["ok"]:
            print(f"  {lbl}: NOT OK {o['errors']}")
            continue
        dev = (o["spot_mid"] / fvn["fv"] - 1.0) * 1e4 if fvn["fv"] else float("nan")
        print(f"  {lbl} ({o['fee_bps']}bp): mark(TWAP) {o['mark_mid']:.5f}  spot {o['spot_mid']:.5f}  "
              f"L {o['L_active']:.3e}  spot-vs-Pyth {dev:+.1f}bp  vol(first-tick window) ${o['interval_vol_usd']:,.0f}")
