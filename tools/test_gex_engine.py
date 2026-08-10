#!/usr/bin/env python3
"""Sanity tests for the GEX engine. Run: python3 test_gex_engine.py"""

import math
import sys
import time
from datetime import date, datetime, timedelta, timezone

import gex_engine
from gex_engine import (
    Contract, bs_gamma, year_fraction, derive_levels, pine_blob, pine_profile,
    strike_gex, contract_size, resolve_size_mode, find_gamma_flip, total_gex_at,
)

# Every level in this file is time-dependent — gamma collapses into a narrower
# and narrower band as expiry approaches, so the same chain produces different
# walls at 09:30 and at 15:30. Pinning "now" is what makes the expectations below
# mean anything; without it the fixture silently becomes a past expiry and the
# whole suite starts measuring the 30-minute time floor instead of the chain.
NOW = datetime(2026, 8, 3, 20, 14, 59, tzinfo=timezone.utc)   # 16:14 ET, 1DTE

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
print("\nBlack-Scholes gamma")
# Reference: live Robinhood quote for SPY 758C exp 2026-08-04, spot 757.63,
# iv 0.112429, quoted 2026-08-03T20:14:59Z. Broker reported gamma 0.091055.
T = year_fraction(date(2026, 8, 4), now=NOW)
g = bs_gamma(757.63, 758.0, T, 0.112429)
check("matches broker greeks within 3%", abs(g - 0.091055) / 0.091055 < 0.03,
      f"bs={g:.6f} broker=0.091055")
check("gamma peaks at the money",
      bs_gamma(100, 100, 0.02, 0.2) > bs_gamma(100, 120, 0.02, 0.2))
check("gamma rises as expiry nears",
      bs_gamma(100, 100, 0.001, 0.2) > bs_gamma(100, 100, 0.05, 0.2))
check("degenerate inputs return 0", bs_gamma(100, 100, 0, 0.2) == 0.0
      and bs_gamma(100, 100, 0.02, 0) == 0.0)
check("0DTE time floor keeps T finite",
      year_fraction(date(2020, 1, 1)) > 0)

# The expiry cut is 16:00 *New York*, which is a different UTC instant in summer
# and winter. A fixed offset was an hour out for the whole EST half of the year,
# and an hour is a large slice of the T left on a 0DTE afternoon.
summer = year_fraction(date(2026, 8, 4), now=datetime(2026, 8, 4, 19, 0, tzinfo=timezone.utc))
winter = year_fraction(date(2026, 1, 15), now=datetime(2026, 1, 15, 20, 0, tzinfo=timezone.utc))
check("expiry cut tracks New York DST, not a fixed offset",
      abs(summer - winter) < 1e-12,
      f"1h to the cut both ways: summer={summer * 8760:.3f}h winter={winter * 8760:.3f}h")

# ---------------------------------------------------------------------------
print("\nSign convention")
T2 = 0.01
call = Contract("call", 100.0, 1000, 0.2)
put = Contract("put", 100.0, 1000, 0.2)
check("calls contribute positive gamma", strike_gex(call, 100.0, T2) > 0)
check("puts contribute negative gamma", strike_gex(put, 100.0, T2) < 0)
check("equal OI at same strike nets to zero",
      abs(strike_gex(call, 100.0, T2) + strike_gex(put, 100.0, T2)) < 1e-6)
check("broker gamma is used when supplied",
      strike_gex(Contract("call", 100.0, 1000, 0.2, gamma=0.05), 100.0, T2)
      == 0.05 * 1000 * 100 * 100 * 100 * 0.01)

# ---------------------------------------------------------------------------
print("\nLevel derivation on a structured chain")
# Spot 757.63. Heavy call OI stacked above (765, 770), heavy put OI below
# (745, 750), so the walls and flip should land in known places.
SPOT = 757.63
EXPIRY = date(2026, 8, 4)
contracts = []
call_oi = {750: 4000, 755: 6000, 758: 9000, 760: 12000, 765: 30000, 770: 22000, 775: 8000}
put_oi = {735: 9000, 740: 14000, 745: 28000, 750: 33000, 755: 11000, 758: 7000, 760: 3000}
for k, oi in call_oi.items():
    contracts.append(Contract("call", float(k), oi, 0.112))
for k, oi in put_oi.items():
    contracts.append(Contract("put", float(k), oi, 0.118))

lv = derive_levels("SPY", EXPIRY, contracts, SPOT, now=NOW)

check("call wall sits above spot", lv.call_wall is not None and lv.call_wall > SPOT,
      f"call_wall={lv.call_wall}")
check("put wall sits below spot", lv.put_wall is not None and lv.put_wall < SPOT,
      f"put_wall={lv.put_wall}")
check("put wall is the heaviest put strike below spot", lv.put_wall == 750.0,
      f"got {lv.put_wall}")
# GEX walls are gamma-weighted, so near expiry they hug spot even when far more
# open interest sits further out — 765 carries 30k OI vs 760's 12k, but 760's
# gamma is large enough to dominate. The OI walls below capture the other view.
check("call wall is gamma-weighted, not OI-weighted", lv.call_wall == 760.0,
      f"got {lv.call_wall} (765 holds more OI but far less gamma)")
check("OI call wall is the heaviest call strike above spot", lv.call_wall_oi == 765.0,
      f"got {lv.call_wall_oi}")
check("OI put wall is the heaviest put strike below spot", lv.put_wall_oi == 750.0,
      f"got {lv.put_wall_oi}")
check("GEX and OI call walls can disagree near expiry",
      lv.call_wall != lv.call_wall_oi)
check("secondary walls populated",
      lv.call_wall_2 is not None and lv.put_wall_2 is not None,
      f"cw2={lv.call_wall_2} pw2={lv.put_wall_2}")
check("control node is the largest |GEX| strike",
      lv.control_node == max(lv.profile, key=lambda kv: abs(kv[1]))[0],
      f"node={lv.control_node}")
check("gamma flip found", lv.gamma_flip is not None, f"flip={lv.gamma_flip}")
check("gamma flip lies between the walls",
      lv.gamma_flip is not None and lv.put_wall < lv.gamma_flip < lv.call_wall,
      f"{lv.put_wall} < {lv.gamma_flip} < {lv.call_wall}")
check("net GEX equals the profile sum",
      abs(lv.net_gex - sum(v for _, v in lv.profile)) < 1e-6)
check("expected move is a sane fraction of spot",
      lv.expected_move is not None and 0 < lv.expected_move < SPOT * 0.05,
      f"em={lv.expected_move:.3f}")
check("profile is sorted by strike",
      [k for k, _ in lv.profile] == sorted(k for k, _ in lv.profile))

# The flip is where aggregate gamma changes sign — verify by construction.
T_FIX = year_fraction(EXPIRY, now=NOW)
below = total_gex_at(contracts, lv.gamma_flip - 3, T_FIX)
above = total_gex_at(contracts, lv.gamma_flip + 3, T_FIX)
check("aggregate gamma actually changes sign across the flip",
      (below < 0) != (above < 0), f"below={below:.3e} above={above:.3e}")

# ---------------------------------------------------------------------------
print("\nGamma flip through the session")
# The failure this guards against: gamma more than a few percent from spot
# underflows to exactly 0.0 in float, and a scan that reads a zero as a sign
# change returns the bottom of its own search window. That put the flip 10%
# below spot for the last two hours of every 0DTE session — which pins the
# regime to "positive gamma" permanently, since price is then always above it.
for label, hours in (("open", 6.4), ("midday", 4.0), ("power hour", 1.5),
                     ("final 30m", 0.5)):
    now = datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc) - timedelta(hours=hours)
    t = year_fraction(EXPIRY, now=now)
    flip = find_gamma_flip(contracts, SPOT, t)
    check(f"0DTE flip stays near spot — {label}",
          flip is not None and abs(flip - SPOT) < SPOT * 0.01,
          f"T={t * 8760:.2f}h flip={flip}")

check("a chain with no sign change reports no flip rather than inventing one",
      find_gamma_flip([Contract("call", 760.0, 5000, 0.15)], SPOT, 0.01) is None)
check("the flip nearest spot wins when several crossings exist",
      abs(find_gamma_flip(contracts, SPOT, T_FIX) - SPOT) < SPOT * 0.02)

# ---------------------------------------------------------------------------
print("\nContract sizing")
c = Contract("call", 760.0, 1000, 0.11, None, 4000)
check("oi mode ignores volume", contract_size(c, "oi") == 1000)
check("volume mode ignores open interest", contract_size(c, "volume") == 4000)
check("max mode takes the larger", contract_size(c, "max") == 4000)
check("sum mode adds them", contract_size(c, "sum") == 5000)
# Open interest is published after the close, so on a 0DTE chain it describes
# yesterday. Today's volume is the only view of what actually traded.
check("auto blends volume in on a 0DTE chain",
      resolve_size_mode("auto", date(2026, 8, 4), NOW.replace(day=4)) == "max")
check("auto trusts open interest further out",
      resolve_size_mode("auto", date(2026, 8, 20), NOW) == "oi")
sized = derive_levels("SPY", EXPIRY, [c], SPOT, size_mode="max", now=NOW)
check("size mode is recorded on the levels", sized.size_mode == "max")
check("size mode reaches the profile",
      sized.profile and sized.profile[0][1] > 0)

# ---------------------------------------------------------------------------
print("\nEmitters")
blob = pine_blob(lv)
prof = pine_profile(lv)
check("blob carries every level key",
      all(f"{k}:" in blob for k in ("flip", "cw", "pw", "cn", "em")), blob)
# The expected move is the move remaining from the spot it was computed at, so
# the band has to hang off that price. Without `ref` the indicator anchors it to
# the session open, and a midday regeneration then draws a half-day band around
# a full-day starting point.
check("blob carries the reference spot for the expected-move band",
      "ref:" in blob and abs(float(dict(p.split(":") for p in blob.split(","))["ref"])
                             - lv.spot) < 0.01)
check("blob carries the open-interest walls",
      "cwo:" in blob and "pwo:" in blob, blob)
check("blob has no spaces (Pine parser strips them anyway)", " " not in blob)
check("profile pairs are strike=value", all("=" in p for p in prof.split(",")))
check("profile is in ascending strike order",
      [float(p.split("=")[0]) for p in prof.split(",")]
      == sorted(float(p.split("=")[0]) for p in prof.split(",")))
check("profile respects the top-N cap", len(pine_profile(lv, top=3).split(",")) == 3)

# Mirror the Pine-side parser to be sure the blob round-trips.
parsed = {}
for part in blob.replace(" ", "").split(","):
    k, _, v = part.partition(":")
    parsed[k] = float(v)
check("flip round-trips through the blob",
      abs(parsed["flip"] - lv.gamma_flip) < 0.01, f"{parsed['flip']} vs {lv.gamma_flip}")
check("call wall round-trips", parsed["cw"] == lv.call_wall)

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
print("\nFutures — Black-76, multipliers, ES/NQ/GC adaptation")
# Verify the futures spec table: the three numbers that matter for GEX correctness
from gex_engine import (
    FUTURES_SPECS, black76_gamma, futures_spec, is_futures, multiplier_for, model_for,
    yf_symbol_for,
)

check("ES is recognised as a future", is_futures("ES"))
check("NQ is recognised as a future", is_futures("NQ"))
check("GC is recognised as a future", is_futures("GC"))
check("SPY is not a future", not is_futures("SPY"))
check("/ES alias resolves", futures_spec("/ES") is not None)
check("ES=F yahoo alias resolves", futures_spec("ES=F") is not None)
check("ES multiplier is $50/pt", multiplier_for("ES") == 50)
check("NQ multiplier is $20/pt", multiplier_for("NQ") == 20)
check("GC multiplier is $100/pt", multiplier_for("GC") == 100)
check("SI multiplier is $5000/pt", multiplier_for("SI") == 5000)
check("CL multiplier is $1000/pt", multiplier_for("CL") == 1000)
check("MGC multiplier is $10/pt", multiplier_for("MGC") == 10)
check("MES multiplier is $5/pt", multiplier_for("MES") == 5)
check("override multiplier wins", multiplier_for("ES", override=100) == 100)
check("ES yahoo symbol is ES=F", yf_symbol_for("ES") == "ES=F")
check("GC yahoo symbol is GC=F", yf_symbol_for("GC") == "GC=F")
check("SPY yahoo pass-through", yf_symbol_for("SPY") == "SPY")
check("ES model is black76", model_for("ES") == "black76")
check("GC model is black76", model_for("GC") == "black76")
check("SPY model is black_scholes", model_for("SPY") == "black_scholes")
check("explicit model override", model_for("ES", override="black_scholes") == "black_scholes")

# Black-76 vs Black-Scholes identity at r=0: should be numerically identical
check("Black-76 at r=0 equals Black-Scholes at r=0",
      abs(black76_gamma(100, 100, 0.02, 0.2, r=0.0) - bs_gamma(100, 100, 0.02, 0.2, r=0.0)) < 1e-12)
# With discount the Black-76 gamma is slightly lower (exp(-rT) factor) — at 0DTE negligible, at 30DTE visible
g76_r0 = black76_gamma(100, 100, 0.08, 0.2, r=0.0)
g76_disc = black76_gamma(100, 100, 0.08, 0.2, r=0.045)
check("Black-76 discount lowers gamma for longer T", g76_disc < g76_r0 and abs(g76_disc/g76_r0 - math.exp(-0.045*0.08)) < 1e-12)

# strike_gex scaling: same gamma, different multiplier → proportional notional
c_test = Contract("call", 100, 1000, 0.2)
gex_100 = strike_gex(c_test, 100, 0.02, multiplier=100, model="black_scholes")
gex_50  = strike_gex(c_test, 100, 0.02, multiplier=50,  model="black_scholes")
check("GEX scales with multiplier (100 vs 50)", abs(gex_50*2 - gex_100) < 1e-6)
# futures GEX uses forward not spot — same inputs should give same gamma at r=0
gex_fut = strike_gex(c_test, 100, 0.02, multiplier=50, model="black76", r=0.0)
check("Black-76 r=0 matches BS at same multiplier", abs(gex_fut - gex_50) < 1e-6)

# Level derivation for futures — the same hedging logic, different price scale
# ES ~ 6000, NQ ~ 21000, GC ~ 2700 — test each headliner the prompt asks for
for sym, spot, strikes, ivs in [
    ("ES", 6000, {5950:5000, 6000:10000, 6050:8000, 6100:4000}, {5950:0.15, 6000:0.15, 6050:0.15, 6100:0.15}),
    ("NQ", 21000, {20800:3000, 21000:8000, 21200:6000}, {20800:0.18, 21000:0.18, 21200:0.18}),
    ("GC", 2700, {2650:2000, 2700:5000, 2750:3000}, {2650:0.14, 2700:0.14, 2750:0.14}),
]:
    conts = []
    for k, oi in strikes.items():
        # mix calls above / puts below so we get walls on both sides
        kind = "call" if k >= spot else "put"
        conts.append(Contract(kind, float(k), oi, ivs[k]))
        # add opposite side too for richer chain
        opp = "put" if kind=="call" else "call"
        conts.append(Contract(opp, float(k), oi//2, ivs[k]))
    lvf = derive_levels(sym, date(2026,8,5), conts, float(spot), now=NOW)
    check(f"{sym} flagged as futures", lvf.is_futures, f"got {lvf.is_futures}")
    check(f"{sym} model is black76", lvf.model == "black76", f"got {lvf.model}")
    check(f"{sym} point value matches spec", lvf.multiplier == FUTURES_SPECS[sym]["multiplier"])
    check(f"{sym} underlying label is forward", lvf.underlying_label == "forward")
    check(f"{sym} call wall above forward", lvf.call_wall is not None and lvf.call_wall >= spot)
    check(f"{sym} put wall below forward", lvf.put_wall is not None and lvf.put_wall <= spot)
    check(f"{sym} expected move sane", lvf.expected_move is not None and 0 < lvf.expected_move < spot*0.05)
    # notional EM in dollars should be reasonable per contract
    if lvf.expected_move and lvf.multiplier:
        notional = abs(lvf.expected_move * lvf.multiplier)
        check(f"{sym} notional EM ~ ${{notional:.0f}} sane", 200 < notional < 50000,
              f"notional ${notional:.0f} for move {lvf.expected_move:.1f} pts * ${lvf.multiplier}/pt")

# Explicit override: force ES to be treated as equity (for SPX-proxy use-case)
lv_es_eq = derive_levels("ES", date(2026,8,5),
                         [Contract("call", 6000, 1000, 0.15), Contract("put", 5950, 1000, 0.15)],
                         6000, now=NOW, model="black_scholes", multiplier=100)
check("forced equity model on futures symbol", lv_es_eq.model=="black_scholes" and not lv_es_eq.is_futures)
check("forced multiplier override", lv_es_eq.multiplier==100)

# Futures total_gex and flip respect multiplier/model
Tfut = year_fraction(date(2026,8,5), now=NOW)
conts_f = [Contract("call", 6000, 5000, 0.15), Contract("put", 5900, 5000, 0.15)]
gf = find_gamma_flip(conts_f, 5950, Tfut, multiplier=50, model="black76", r=0.045)
check("futures gamma flip found", gf is not None)
# At r=0 the flip should be nearly identical between models (discount is tiny at 0DTE)
gf_r0_76 = find_gamma_flip(conts_f, 5950, Tfut, multiplier=50, model="black76", r=0.0)
gf_bs    = find_gamma_flip(conts_f, 5950, Tfut, multiplier=50, model="black_scholes", r=0.0)
check("futures vs BS flip close at r=0", gf_r0_76 is not None and gf_bs is not None and abs(gf_r0_76 - gf_bs) < 2.0)

# Pine blob for futures carries mult and mdl keys (backward-compatible — old Pine ignores them)
lv_es_test = derive_levels("ES", date(2026,8,5),
                           [Contract("call", 6050, 5000, 0.15), Contract("put", 5950, 5000, 0.15)],
                           6000, now=NOW)
blob_f = pine_blob(lv_es_test)
check("futures blob carries mult", "mult:" in blob_f, blob_f)
check("futures blob carries mdl", "mdl:" in blob_f, blob_f)
check("equity blob does not carry mult", "mult:" not in pine_blob(lv), pine_blob(lv))

# Dashboard payload and rendering for futures
from dashboard import render_dashboard, payload_for
pl_f = payload_for([lv_es_test])
check("futures payload carries multiplier", pl_f[0]["multiplier"] == 50)
check("futures payload is_futures true", pl_f[0]["is_futures"] is True)
html_f = render_dashboard([lv_es_test])
check("futures dashboard shows FUT badge", "FUT" in html_f and "badge-fut" in html_f and "forward" in html_f)
check("futures dashboard no external requests", "http://" not in html_f)
# Mixed dashboard (equity + futures) tabs both present
html_mixed = render_dashboard([lv, lv_es_test])
check("mixed dashboard has both symbols", "SPY" in html_mixed and "ES" in html_mixed)


print("\nSymbol resolution — futures roots vs equity tickers")
# Substring matching on the ticker is the trap here, and the Pine side was
# falling into it: "PL" is inside AAPL, "SI" inside SIRI, "ES" inside AES and
# MESA, "CL" inside CLF, "BTC" inside BTCS. Every one of those is an ordinary
# stock that must stay on Black-Scholes with a 100-share contract.
for tkr in ["AAPL", "SIRI", "AES", "MESA", "CLF", "BTCS", "GCI", "PLTR",
            "NGG", "ZBH", "RBLX", "HOOD", "SPY", "QQQ", "ETHE"]:
    check(f"{tkr} is not treated as futures",
          not gex_engine.is_futures(tkr)
          and gex_engine.multiplier_for(tkr) == 100
          and gex_engine.model_for(tkr) == "black_scholes")

# The micros must resolve to their own point value, not their big brother's.
for root, mult in [("ES", 50), ("MES", 5), ("NQ", 20), ("MNQ", 2),
                   ("GC", 100), ("MGC", 10), ("CL", 1000), ("MCL", 100),
                   ("YM", 5), ("MYM", 0.5), ("RTY", 50), ("M2K", 5)]:
    check(f"{root} point value is ${mult:g}", gex_engine.multiplier_for(root) == mult,
          f"got {gex_engine.multiplier_for(root)}")

# Every spelling of the same contract has to land on one spec.
for spelling in ["ES", "/ES", "ES=F", "ES1!", "ES2!", "es"]:
    check(f"'{spelling}' resolves to the ES contract",
          gex_engine.multiplier_for(spelling) == 50 and gex_engine.is_futures(spelling))

# 2Y notes are $200k face where the rest of the curve is $100k, so a full
# point is $2000 and not $1000 — the value the whole strip had been given.
check("ZT is $2000/pt (200k face), not $1000",
      gex_engine.multiplier_for("ZT") == 2000, f"got {gex_engine.multiplier_for('ZT')}")
check("ZF/ZN/ZB stay at $1000/pt",
      all(gex_engine.multiplier_for(s) == 1000 for s in ("ZF", "ZN", "ZB")))

# ---------------------------------------------------------------------------
print("\nEdge cases")
empty = derive_levels("XYZ", EXPIRY, [], 100.0)
check("empty chain yields empty levels, no crash",
      empty.call_wall is None and empty.net_gex == 0 and empty.profile == [])
zero_oi = derive_levels("XYZ", EXPIRY, [Contract("call", 100.0, 0, 0.2)], 100.0)
check("zero-OI contracts are skipped", zero_oi.profile == [])
one_sided = derive_levels("XYZ", EXPIRY, [Contract("call", 110.0, 500, 0.2)], 100.0)
check("call-only chain has no put wall and no crash", one_sided.put_wall is None)

# ---------------------------------------------------------------------------
print("\nDashboard")
from dashboard import render_dashboard
html = render_dashboard([lv])
check("renders a complete HTML document",
      html.startswith("<!doctype html>") and html.rstrip().endswith("</html>"))
check("data is embedded, no external requests",
      "http://" not in html and "https://" not in html)
check("payload reached the page", '"symbol": "SPY"' in html or '"symbol":"SPY"' in html)
check("both theme scopes present",
      "prefers-color-scheme: dark" in html and '[data-theme="dark"]' in html)
check("static page is not in live mode", "const LIVE = false" in html)

from dashboard import payload_for
pl = payload_for([lv])
check("payload carries blobs for the API",
      pl[0]["pine_blob"] and pl[0]["pine_profile"])
check("payload profile is JSON-safe pairs",
      isinstance(pl[0]["profile"], list) and isinstance(pl[0]["profile"][0], list))

live_html = render_dashboard([lv], live=True, poll_ms=30000)
check("live mode sets the flag and interval",
      "const LIVE = true" in live_html and "POLL_MS = 30000" in live_html)
check("live mode renders the LIVE badge", 'class="live"' in live_html)

# ---------------------------------------------------------------------------
print("\nLive server")
import json as _json
import threading
import urllib.request

_orig_loader = gex_engine.load_yfinance
_ticks = {"n": 0}


def _fake_chain(sym, dte):
    _ticks["n"] += 1
    cs = [Contract("call", float(k), oi, 0.112)
          for k, oi in {750: 4000, 760: 12000, 765: 30000}.items()]
    cs += [Contract("put", float(k), oi, 0.118)
           for k, oi in {745: 28000, 750: 33000}.items()]
    return cs, 757.63 + _ticks["n"], date(2026, 8, 4)


gex_engine.load_yfinance = _fake_chain
try:
    port = 8799
    threading.Thread(target=lambda: gex_engine.run_server(["SPY"], 0, port, 3),
                     daemon=True).start()
    deadline = time.time() + 20
    body = None
    while time.time() < deadline:
        try:
            body = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/levels",
                                          timeout=2).read()
            break
        except Exception:
            time.sleep(0.4)
    check("server answers /api/levels", body is not None)
    if body:
        api = _json.loads(body)
        check("api returns one symbol", len(api["levels"]) == 1)
        check("api reports no error", api["error"] is None, str(api["error"]))
        check("api is timestamped", bool(api["updated"]))
        page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2).read().decode()
        check("server serves the live page",
              page.startswith("<!doctype html>") and "const LIVE = true" in page)
        first = api["levels"][0]["spot"]
        time.sleep(4)                                  # let one refresh land
        api2 = _json.loads(urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/levels", timeout=2).read())
        check("levels actually refresh on the interval",
              api2["levels"][0]["spot"] != first,
              f"{first} → {api2['levels'][0]['spot']}")
finally:
    gex_engine.load_yfinance = _orig_loader

# ---------------------------------------------------------------------------
print("\nLive server resilience")
# One symbol failing used to discard the whole batch, so a momentary 404 on IWM
# blanked SPY as well. Each symbol keeps its own last good snapshot now.
_fail = {"on": False}


def _flaky(sym, dte):
    if sym == "BAD" or _fail["on"]:
        raise RuntimeError("chain unavailable")
    return ([Contract("call", 105.0, 900, 0.2), Contract("put", 95.0, 900, 0.2)],
            100.0, date(2026, 8, 4))


_saved = gex_engine.load_yfinance
gex_engine.load_yfinance = _flaky
try:
    port2 = 8801
    threading.Thread(target=lambda: gex_engine.run_server(["SPY", "BAD"], 0, port2, 3),
                     daemon=True).start()
    deadline = time.time() + 20
    api = None
    while time.time() < deadline:
        try:
            api = _json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{port2}/api/levels", timeout=2).read())
            break
        except Exception:
            time.sleep(0.4)
    check("a failing symbol does not take the healthy ones down with it",
          api is not None and len(api["levels"]) == 1
          and api["levels"][0]["symbol"] == "SPY",
          str(api and [l["symbol"] for l in api["levels"]]))
    check("the failure is reported rather than swallowed",
          api is not None and api["error"] and "BAD" in api["error"], str(api and api["error"]))
    # Now break everything: the last good snapshot has to survive.
    _fail["on"] = True
    time.sleep(4)
    api2 = _json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{port2}/api/levels", timeout=2).read())
    check("stale data keeps serving when every refresh fails",
          len(api2["levels"]) == 1 and api2["levels"][0]["symbol"] == "SPY")
finally:
    gex_engine.load_yfinance = _saved

# ---------------------------------------------------------------------------
print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all checks passed")
