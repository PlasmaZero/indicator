#!/usr/bin/env python3
"""Sanity tests for the GEX engine. Run: python3 test_gex_engine.py"""

import math
import sys
from datetime import date, datetime, timezone

from gex_engine import (
    Contract, bs_gamma, year_fraction, derive_levels, pine_blob, pine_profile,
    strike_gex,
)

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------------------
print("\nBlack-Scholes gamma")
# Reference: live Robinhood quote for SPY 758C exp 2026-08-04, spot 757.63,
# iv 0.112429, quoted 2026-08-03T20:14:59Z. Broker reported gamma 0.091055.
T = year_fraction(date(2026, 8, 4), now=datetime(2026, 8, 3, 20, 14, 59, tzinfo=timezone.utc))
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

lv = derive_levels("SPY", EXPIRY, contracts, SPOT)

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
from gex_engine import total_gex_at
below = total_gex_at(contracts, lv.gamma_flip - 3, year_fraction(EXPIRY))
above = total_gex_at(contracts, lv.gamma_flip + 3, year_fraction(EXPIRY))
check("aggregate gamma actually changes sign across the flip",
      (below < 0) != (above < 0), f"below={below:.3e} above={above:.3e}")

# ---------------------------------------------------------------------------
print("\nEmitters")
blob = pine_blob(lv)
prof = pine_profile(lv)
check("blob carries every level key",
      all(f"{k}:" in blob for k in ("flip", "cw", "pw", "cn", "em")), blob)
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

# ---------------------------------------------------------------------------
print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all checks passed")
