# @purpose live_render.py - live_sim_plan.html S3: the watch UI for the forward-press LP. Renders a
#          self-contained, offline-portable live_book.html (data inlined, same Apollo skin as the control
#          panel) regenerated every tick by live_lp.py. Four panels (live_sim_plan):
#            TOP        - live equity curve since inception (inline SVG, no libs) + NAV vs the TWO
#                         baselines (F8): HODL-the-inception-basket (IL peer) and Aave-USDC; net APR
#                         realized-to-date (annualized, single-week caveat); liveness lamp (last-tick age).
#            PER POOL   - live mid + band (in/out-of-range), position value, fees, LVR-diag, net P&L,
#                         time-in-range %, recenters.
#            DECISION   - append-only stream of the agent's autonomous moves (allocate / reallocate /
#                         recenter / breaker freeze) read from the ledger - "watch it play out."
#            DQ LAMPS   - feed freshness, mid~Pyth basis, FX-CLOSED/STALE/DISLOCATED breaker, vol source.
#          <meta http-equiv=refresh> so an open tab updates itself. A JS shim ages the liveness lamp live.
#
#          Read-only renderer. Paper tool - NAV is notional, no capital.

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE = Path(os.environ.get("ONCHAIN_FX_LIVE_DIR", ROOT / "artifacts" / "book" / "live")).resolve()
LIVE.mkdir(parents=True, exist_ok=True)
# In the public repo set ONCHAIN_FX_LIVE_OUT=live.html so the watch UI publishes at the Pages tab URL.
OUT = LIVE / os.environ.get("ONCHAIN_FX_LIVE_OUT", "live_book.html")
LEDGER = LIVE / "live_ledger.jsonl"
# Cross-link target for the nav tab (the control-panel dashboard). Same-repo relative path on Pages.
DASHBOARD_HREF = os.environ.get("ONCHAIN_FX_DASHBOARD_HREF", "index.html")
SEC_Y = 365.0 * 24 * 3600.0
REFRESH_S = 120


def _fmt_ts(ts) -> str:
    if ts is None:
        return "--"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _svg_curve(hist: list, w: int = 940, h: int = 240, pad: int = 38) -> str:
    """Inline SVG equity curve: NAV (gold) vs HODL (blue) vs Aave (faint) over nav_hist, with a $10k
    reference line. Downsamples to ~320 points. No external libs - offline portable."""
    if len(hist) < 2:
        return ('<div class="empty">equity curve appears after the first marked tick '
                '(inception + 1)</div>')
    step = max(1, len(hist) // 320)
    pts = hist[::step]
    if pts[-1] is not hist[-1]:
        pts = pts + [hist[-1]]
    xs = [p["ts"] for p in pts]
    series = {"nav": [p["nav"] for p in pts], "hodl": [p["hodl"] for p in pts], "aave": [p["aave"] for p in pts]}
    t0, t1 = min(xs), max(xs)
    allv = series["nav"] + series["hodl"] + series["aave"] + [pts[0]["nav"]]
    lo, hi = min(allv), max(allv)
    span = (hi - lo) or 1.0
    lo -= span * 0.08
    hi += span * 0.08
    span = hi - lo

    def X(t):
        return pad + (t - t0) / ((t1 - t0) or 1) * (w - 2 * pad)

    def Y(v):
        return h - pad - (v - lo) / span * (h - 2 * pad)

    def path(vals, color, wdt, dash=""):
        d = "M" + " L".join(f"{X(x):.1f},{Y(v):.1f}" for x, v in zip(xs, vals))
        da = f' stroke-dasharray="{dash}"' if dash else ""
        return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{wdt}"{da}/>'

    dep = pts[0]["nav"]
    refy = Y(dep)
    grid = f'<line x1="{pad}" y1="{refy:.1f}" x2="{w-pad}" y2="{refy:.1f}" stroke="#4a5158" stroke-width="1" stroke-dasharray="2,4"/>'
    grid += f'<text x="{w-pad+2}" y="{refy+3:.1f}" fill="#7a7d76" font-size="10" font-family="ui-monospace,Menlo,monospace">${dep:,.0f}</text>'
    # y-axis ticks (3)
    for frac in (0.0, 0.5, 1.0):
        v = lo + span * frac
        yy = Y(v)
        grid += (f'<text x="4" y="{yy+3:.1f}" fill="#7a7d76" font-size="10" '
                 f'font-family="ui-monospace,Menlo,monospace">${v:,.0f}</text>')
    # x-axis end labels
    grid += (f'<text x="{pad}" y="{h-8}" fill="#7a7d76" font-size="10" font-family="ui-monospace,Menlo,monospace">'
             f'{datetime.fromtimestamp(t0,timezone.utc):%m-%d %H:%MZ}</text>')
    grid += (f'<text x="{w-pad}" y="{h-8}" fill="#7a7d76" font-size="10" text-anchor="end" '
             f'font-family="ui-monospace,Menlo,monospace">{datetime.fromtimestamp(t1,timezone.utc):%m-%d %H:%MZ}</text>')
    body = (grid
            + path(series["aave"], "#7a7d76", 1.3, "4,3")
            + path(series["hodl"], "#8ab4d8", 1.6)
            + path(series["nav"], "#d8b86a", 2.2))
    return f'<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="xMidYMid meet">{body}</svg>'


def _decision_rows(limit: int = 45) -> str:
    if not LEDGER.exists():
        return '<div class="empty">no decisions yet</div>'
    rows = []
    for line in LEDGER.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        for a in r.get("actions", []):
            rows.append((r["ts"], r.get("breaker"), a))
    rows = rows[-limit:][::-1]
    out = []
    for ts, breaker, a in rows:
        t = a["type"]
        if t == "ALLOCATE":
            cls, label = "g", "ALLOCATE"
            rk = " · ".join(f"{x['pool'].split('_')[1]} {x['net_apr']*100:+.1f}%" for x in a.get("ranking", []))
            detail = f"open book → <b>{a['pool']}</b> @ {a['mid']:.5f} (net {(a.get('net_apr') or 0)*100:+.1f}%) &nbsp; [{rk}]"
        elif t == "REALLOCATE":
            cls, label = "gd", "REALLOCATE"
            detail = (f"<b>{a['from']}</b> → <b>{a['to']}</b> @ {a['at_mid']:.5f} "
                      f"(net gain {(a.get('gain_apr') or 0)*100:+.1f}%/yr, switch ${a['gas']+a['basis_cost']:.3f})")
        elif t == "RECENTER":
            cls, label = "b", "RECENTER"
            detail = f"<b>{a['pool']}</b> band → [{a['new_p_a']:.4f}, {a['new_p_b']:.4f}] @ {a['at_mid']:.5f} (gas ${a['gas']:.3f})"
        elif t == "FREEZE":
            cls, label = "r", "FREEZE"
            detail = a["reason"]
        elif t == "SCAN_DECLINE":
            cls, label = "f", "scan"
            rk = " · ".join(f"{x['pool'].split('_')[1]} {x['net_apr']*100:+.1f}%" for x in a.get("ranking", []))
            detail = (f"hold <b>{a['held']}</b> (net {(a.get('net_now') or 0)*100:+.1f}%); "
                      f"best {a['best']} — no move clears the hurdle &nbsp; [{rk}]")
        else:
            cls, label, detail = "f", t, json.dumps(a)
        out.append(f'<tr><td class="dt">{_fmt_ts(ts)}</td><td><span class="tag {cls}">{label}</span></td>'
                   f'<td class="dd">{detail}</td></tr>')
    return "".join(out)


def _lamp(state: str, label: str) -> str:
    color = {"OK": "g", "INIT": "f", "FX_CLOSED": "a", "DISLOCATED": "a", "STALE": "r"}.get(state, "f")
    return f'<span class="lamp {color}">●</span> {label}'


def render(state: dict, last_row: dict) -> None:
    pos = state.get("position")
    inc_ts = state["inception_ts"]
    last_ts = state.get("last_tick_ts")
    br = state.get("breaker", {})
    hist = state.get("nav_hist", [])
    dep = state["book_usd"]

    # headline marks
    if hist:
        m = hist[-1]
        nav, hodl, aave, mid = m["nav"], m["hodl"], m["aave"], m["mid"]
    else:
        nav = hodl = aave = dep
        mid = pos["entry_mid"] if pos else float("nan")
    yrs = (last_ts - inc_ts) / SEC_Y if last_ts else 0.0
    vs_hodl = nav - hodl
    vs_aave = nav - aave
    # Don't annualize a sub-day window — a few minutes of noise annualizes to absurd ±1000s of %.
    # The single-window caveat "decays as the run lengthens": show period P&L until >=1 day elapsed.
    MIN_ANNUALIZE_YRS = 1.0 / 365.0
    if yrs >= MIN_ANNUALIZE_YRS:
        apr = (nav / dep - 1.0) / yrs
        apr_val, apr_sub, apr_cls = f"{apr*100:+.1f}%", "annualized · single-window caveat", "good" if apr >= 0 else "bad"
    else:
        days = yrs * 365.0
        apr_val = f"{nav-dep:+,.2f}"
        apr_sub = f"period P&L · annualizes after 1d (run {days*24:.1f}h)"
        apr_cls = "good" if nav >= dep else "bad"

    def card(label, val, sub, cls=""):
        return (f'<div class="card {cls}"><div class="cl">{label}</div>'
                f'<div class="cv">{val}</div><div class="cs">{sub}</div></div>')

    headline = "".join([
        card("Paper NAV", f"${nav:,.2f}", f"on ${dep:,.0f} · {state['n_ticks']} ticks"),
        card("vs HODL basket", f"{vs_hodl:+,.2f}", "strategy alpha (IL peer)", "good" if vs_hodl >= 0 else "bad"),
        card("vs Aave USDC", f"{vs_aave:+,.2f}", f"do-nothing @ {4.5:.1f}%", "good" if vs_aave >= 0 else "bad"),
        card("P&L / Net APR", apr_val, apr_sub, apr_cls),
    ])

    # per-pool panel (held pool detailed; others as scanner candidates)
    pool_rows = ""
    if pos:
        tir = pos["ticks_in_range"] / max(1, pos["n_ticks"]) * 100
        in_range = pos["p_a"] <= mid <= pos["p_b"]
        band_pct = (mid - pos["p_a"]) / (pos["p_b"] - pos["p_a"]) * 100 if pos["p_b"] > pos["p_a"] else 50
        band_pct = max(2, min(98, band_pct))
        rng_lamp = '<span class="lamp g">●</span> in range' if in_range else '<span class="lamp a">●</span> OUT of range'
        bar = (f'<div class="band"><div class="bt">{pos["p_a"]:.4f}</div>'
               f'<div class="btrack"><div class="bmark" style="left:{band_pct:.1f}%"></div></div>'
               f'<div class="bt">{pos["p_b"]:.4f}</div></div>')
        net_pnl = pos["fees_usd"] - pos["gas_usd"] + (nav - hodl - (pos["fees_usd"] - pos["gas_usd"]))
        pool_rows = f"""
        <div class="poolhead"><b>{pos['pool']}</b> · {state['corridor'].upper()} · ±{pos['half_range']*100:.0f}% band &nbsp; {rng_lamp}</div>
        {bar}
        <table class="kv">
          <tr><td>live mid (TWAP)</td><td class="num">{mid:.5f}</td><td>position value</td><td class="num">${nav - pos['fees_usd'] + pos['gas_usd']:,.2f}</td></tr>
          <tr><td>fees accrued</td><td class="num good">${pos['fees_usd']:.3f}</td><td>LVR charged (diag)</td><td class="num bad">${pos['lvr_usd']:.3f}</td></tr>
          <tr><td>time in range</td><td class="num">{tir:.0f}%</td><td>recenters</td><td class="num">{pos['n_recenters']}</td></tr>
          <tr><td>gas paid</td><td class="num">${pos['gas_usd']:.3f}</td><td>reallocations</td><td class="num">{state['n_reallocations']}</td></tr>
        </table>"""

    # scanner ranking (from last action that carries a ranking)
    rank_rows = ""
    last_rank = None
    for a in reversed(last_row.get("actions", [])):
        if a.get("ranking"):
            last_rank = a["ranking"]
            break
    if last_rank:
        for r in last_rank:
            held = pos and r["pool"] == pos["pool"]
            inelig = not r.get("eligible", True)
            # screened (thin-TVL) pools are watched for visibility but never allocated — flag them, and
            # cap the displayed APR so a wash-inflated >100% gross reads as a warning, not an opportunity.
            def _apr(x):
                return ">+100%" if x > 1.0 else ("<-100%" if x < -1.0 else f"{x*100:+.1f}%")
            tag = ' <span class="flag">⚑ screened · thin TVL</span>' if inelig else (" ◂ held" if held else "")
            cls = "held" if held else ("inelig" if inelig else "")
            rank_rows += (f'<tr{(" class="+cls) if cls else ""}><td>{r["pool"]}{tag}</td>'
                          f'<td class="num">{r["fee_bps"]:.0f}bp</td>'
                          f'<td class="num">{r["fee_share"]*100:.4f}%</td>'
                          f'<td class="num">{_apr(r["gross_apr"])}</td>'
                          f'<td class="num">{r["lvr_apr"]*100:.1f}%</td>'
                          f'<td class="num {"" if inelig else ("good" if r["net_apr"]>=0 else "bad")}">{_apr(r["net_apr"])}</td></tr>')

    # DQ lamps
    dev = last_row.get("dev_bps")
    fv = last_row.get("fv")
    sig = last_row.get("sigma", 0)
    sig_src = "live-blended" if state["sigma"]["n"] > 0 and br.get("state") != "FX_CLOSED" else "2y prior"
    lamps = "".join([
        f'<div>{_lamp(br.get("state","INIT"), "breaker: " + br.get("state","INIT"))}</div>',
        f'<div><span class="lamp {"g" if fv else "a"}">●</span> Pyth FV: {("$"+format(fv,".5f")) if fv else "stale (FX-closed)"}</div>',
        f'<div><span class="lamp {"g" if (dev is not None and abs(dev)<25) else ("a" if dev is not None else "f")}">●</span> '
        f'mid≈Pyth: {(format(dev,"+.1f")+"bp") if dev is not None else "n/a"}</div>',
        f'<div><span class="lamp g">●</span> σ: {sig*100:.1f}% ({sig_src})</div>',
        f'<div><span class="lamp g">●</span> gas: {last_row.get("gas_gwei",float("nan")):.4f} gwei</div>',
        f'<div id="liveness"><span class="lamp f">●</span> last tick: <span id="age">{_fmt_ts(last_ts)}</span></div>',
    ])

    curve = _svg_curve(hist)
    decisions = _decision_rows()
    breaker_banner = ""
    if br.get("state") in ("STALE", "DISLOCATED", "FX_CLOSED"):
        bc = {"STALE": "r", "DISLOCATED": "a", "FX_CLOSED": "a"}[br["state"]]
        breaker_banner = (f'<div class="banner {bc}">⚠ {br["state"]} — {br.get("reason","")}. '
                          f'The agent is holding (no recenter / no reallocation) on this state.</div>')

    html = f"""<!DOCTYPE html>
<!-- @purpose live_book.html - generated by live_render.py every tick. Onchain FX live paper LP watch UI.
     Self-contained, offline-portable. Paper only - NAV notional, no capital, no on-chain actions. -->
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{REFRESH_S}">
<title>Onchain FX · Live LP · Watch</title>
<style>
:root{{--bg:#16181a;--panel:#1d2023;--panel2:#23272b;--edge:#33383d;--edge2:#4a5158;--ink:#e8e6df;
--dim:#a8aaa2;--faint:#7a7d76;--green:#7fd49a;--amber:#e8c170;--red:#e07856;--blue:#8ab4d8;--gold:#d8b86a;
--mono:ui-monospace,'SF Mono',Menlo,monospace;}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,'Helvetica Neue',sans-serif;padding:0 0 70px}}
.wrap{{max-width:1000px;margin:0 auto;padding:0 24px}}
header{{border-bottom:1px solid var(--edge);padding:26px 0 16px;margin-bottom:18px}}
h1{{font-size:23px;margin:0;letter-spacing:.01em}} h1 .v{{color:var(--gold)}}
.meta{{font-family:var(--mono);font-size:11.5px;color:var(--faint);margin-top:7px;line-height:1.7}}
h2{{font-size:14px;margin:30px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--edge);
font-family:var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--dim);font-weight:600}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0}}
.card{{background:var(--panel);border:1px solid var(--edge);border-radius:7px;padding:13px 15px}}
.card.good{{border-left:3px solid var(--green)}} .card.bad{{border-left:3px solid var(--red)}}
.cl{{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint)}}
.cv{{font-size:23px;font-family:var(--mono);margin:5px 0 2px;color:var(--ink)}}
.cs{{font-size:11.5px;color:var(--dim)}}
.card.good .cv{{color:var(--green)}} .card.bad .cv{{color:var(--red)}}
.chart{{background:var(--panel);border:1px solid var(--edge);border-radius:7px;padding:12px 8px 4px;margin:12px 0}}
.legend{{font-family:var(--mono);font-size:11px;color:var(--faint);padding:2px 10px 8px;display:flex;gap:18px}}
.legend b{{font-weight:400}} .lg{{color:var(--gold)}} .lb{{color:var(--blue)}} .lf{{color:var(--faint)}}
.empty{{color:var(--faint);font-style:italic;padding:30px 12px;text-align:center;font-size:13px}}
.grid2{{display:grid;grid-template-columns:1.05fr .95fr;gap:16px}}
@media(max-width:780px){{.cards{{grid-template-columns:repeat(2,1fr)}}.grid2{{grid-template-columns:1fr}}}}
.poolhead{{font-size:13.5px;margin:4px 0 10px;color:var(--dim)}}
.band{{display:flex;align-items:center;gap:10px;margin:6px 0 14px}}
.bt{{font-family:var(--mono);font-size:11px;color:var(--faint)}}
.btrack{{flex:1;height:8px;background:var(--panel2);border:1px solid var(--edge);border-radius:5px;position:relative}}
.bmark{{position:absolute;top:-3px;width:3px;height:12px;background:var(--gold);border-radius:2px;transform:translateX(-50%)}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
.kv td{{padding:6px 8px;border-bottom:1px solid #2a2e32;color:var(--dim)}}
.kv td:nth-child(odd){{color:var(--faint);font-size:11.5px;font-family:var(--mono);width:26%}}
.num{{font-family:var(--mono);text-align:right}}
th{{text-align:left;padding:6px 8px;color:var(--faint);font-family:var(--mono);font-size:10px;
letter-spacing:.07em;text-transform:uppercase;border-bottom:1px solid var(--edge)}}
td{{padding:6px 8px;border-bottom:1px solid #2a2e32;color:var(--dim)}}
tr.held td{{color:var(--ink)}} tr.held{{background:rgba(216,184,106,.06)}}
tr.inelig td{{color:var(--faint)}} tr.inelig{{opacity:.72}}
.flag{{font-family:var(--mono);font-size:9.5px;letter-spacing:.04em;color:var(--amber);border:1px solid var(--amber);border-radius:3px;padding:0 4px;margin-left:5px;white-space:nowrap}}
.good{{color:var(--green)}} .bad{{color:var(--red)}}
.dec{{background:var(--panel);border:1px solid var(--edge);border-radius:7px;padding:4px 14px;max-height:340px;overflow-y:auto}}
.dec td{{font-size:12.5px;vertical-align:top}}
.dec .dt{{font-family:var(--mono);font-size:10.5px;color:var(--faint);white-space:nowrap;width:140px}}
.dec .dd{{color:var(--dim)}}
.tag{{font-family:var(--mono);font-size:10px;padding:1px 6px;border-radius:3px;border:1px solid currentColor;text-transform:uppercase}}
.tag.g{{color:var(--green)}} .tag.gd{{color:var(--gold)}} .tag.b{{color:var(--blue)}} .tag.r{{color:var(--red)}} .tag.a{{color:var(--amber)}} .tag.f{{color:var(--faint)}}
.lamp{{font-size:11px;vertical-align:1px}} .lamp.g{{color:var(--green)}} .lamp.a{{color:var(--amber)}} .lamp.r{{color:var(--red)}} .lamp.f{{color:var(--faint)}}
.lamps{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px 18px;background:var(--panel);
border:1px solid var(--edge);border-radius:7px;padding:13px 16px;font-size:12.5px;color:var(--dim);font-family:var(--mono)}}
.banner{{border-radius:7px;padding:11px 15px;margin:12px 0;font-size:13px;border:1px solid currentColor}}
.banner.r{{color:var(--red);background:rgba(224,120,86,.08)}} .banner.a{{color:var(--amber);background:rgba(232,193,112,.08)}}
.foot{{margin-top:40px;padding-top:14px;border-top:1px solid var(--edge);font-family:var(--mono);
font-size:10.5px;color:var(--faint);line-height:1.8}}
.nav{{display:flex;gap:4px;padding:14px 0 0}}
.nav a{{font-family:var(--mono);font-size:12px;letter-spacing:.04em;text-decoration:none;color:var(--dim);
padding:7px 15px;border:1px solid var(--edge);border-bottom:none;border-radius:6px 6px 0 0;background:var(--panel2)}}
.nav a:hover{{color:var(--ink)}}
.nav a.on{{color:var(--gold);background:var(--bg);border-color:var(--edge2);border-bottom:1px solid var(--bg);position:relative;top:1px}}
</style></head>
<body><div class="wrap">
<nav class="nav"><a href="{DASHBOARD_HREF}">Market Structure</a><a class="on" href="#">Live LP ●</a></nav>
<header style="border-top:1px solid var(--edge);padding-top:22px">
  <h1>Onchain FX <span class="v">· Live LP · Watch</span></h1>
  <div class="meta">autonomous paper LP pressed forward against live Base feeds · {state['corridor'].upper()} on Base ·
  inception {_fmt_ts(inc_ts)} · last tick {_fmt_ts(last_ts)} · auto-refresh {REFRESH_S}s<br>
  PAPER ONLY — NAV is notional, no capital, no on-chain actions. Engine reused unchanged (book.py / sim.py, adversarially reviewed); live layer = feed + state + scanner + breaker.</div>
</header>

{breaker_banner}

<h2>Headline — forward equity vs the two baselines</h2>
<div class="cards">{headline}</div>
<div class="chart">{curve}
  <div class="legend"><b class="lg">━ NAV (LP)</b><b class="lb">━ HODL basket</b><b class="lf">╌ Aave USDC</b></div>
</div>

<div class="grid2">
  <div>
    <h2>Held position</h2>
    {pool_rows or '<div class="empty">no position yet</div>'}
  </div>
  <div>
    <h2>Scanner — net-APR ranking (this tick)</h2>
    <table><tr><th>pool</th><th>tier</th><th>L-share</th><th>gross</th><th>LVR</th><th>net APR</th></tr>{rank_rows or '<tr><td colspan=6 class=empty>—</td></tr>'}</table>
    <div class="meta" style="margin-top:8px">Ranks on <b>NET</b> (gross fee − parametric LVR), fee_share in v3 L-units (F2). Reallocates only past the switch-cost hurdle (payback ≤30d + 50bp hysteresis). Mostly declines at $10k — the edge is structural. <b>⚑ screened</b> pools (TVL &lt; $300k) are <b>watched but not allocated</b> — their volume/TVL implies a wash-inflated APR a $10k LP can't actually earn.</div>
  </div>
</div>

<h2>Decision log — the agent's autonomous moves</h2>
<div class="dec"><table>{decisions}</table></div>

<h2>Data-quality lamps</h2>
<div class="lamps">{lamps}</div>

<div class="foot">
  live_render.py · {state['corridor'].upper()} on Base · band ±{(pos['half_range']*100 if pos else 3):.0f}% · σ blended (2y prior shrinking toward run-realized TWAP vol, F3) ·
  fee_share in v3 L-units (F2) · LVR parametric @ σ (diagnostic, not subtracted from NAV) · basis a discrete realloc cost (F4) ·
  TWAP mark via observe() (F1) · self-healing block cursor (F5) · two baselines (F8).<br>
  Reuses book.py (LP primitives) · sim.py (Elsts geometry, Ledger) · pyth.py (FV) · live_mid.py (Base RPC). Paper only.
</div>
</div>
<script>
// age the liveness lamp live in the browser (Actions cron is best-effort, F11)
(function(){{
  var last={last_ts if last_ts else 0}*1000;
  function tick(){{
    var el=document.getElementById('age'); if(!el||!last) return;
    var age=Math.floor((Date.now()-last)/1000);
    var lamp=document.querySelector('#liveness .lamp');
    var txt=age<90?age+'s ago':(age<5400?Math.floor(age/60)+'m ago':Math.floor(age/3600)+'h ago');
    el.textContent=txt+' ('+new Date(last).toISOString().slice(0,19)+'Z)';
    if(lamp) lamp.className='lamp '+(age<1800?'g':(age<5400?'a':'r'));
  }}
  tick(); setInterval(tick,1000);
}})();
</script>
</body></html>"""
    OUT.write_text(html)


if __name__ == "__main__":
    sp = LIVE / "state.json"
    if sp.exists():
        st = json.loads(sp.read_text())
        rows = [json.loads(l) for l in LEDGER.read_text().splitlines() if l.strip()] if LEDGER.exists() else []
        render(st, rows[-1] if rows else {})
        print(f"rendered -> {OUT}")
    else:
        print("no state.json yet — run live_lp.py tick first")
