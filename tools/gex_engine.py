#!/usr/bin/env python3
"""
GEX engine — turn an options chain into dealer gamma-exposure levels.

Computes gamma exposure per strike, locates the call wall, put wall, gamma flip
(zero-gamma / HVL) and control node, then emits:

  * a JSON file of levels
  * a paste-ready blob for the Pine indicator
  * a self-contained HTML dashboard

Supports both equity index options (SPY, QQQ — Black-Scholes on spot) and
futures options on the CME (ES, NQ, GC, CL … — Black-76 on the forward) with
the correct contract multipliers and session handling.

Usage
-----
    # live chain via yfinance (pip install yfinance)
    python3 gex_engine.py --symbols SPY,QQQ,IWM,TSLA,NVDA --dte 0
    python3 gex_engine.py --symbols ES,NQ,GC --dte 0

    # force futures treatment or override the point value
    python3 gex_engine.py --symbols ES --model black76 --multiplier 50

    # bring your own chain export
    python3 gex_engine.py --source csv --csv chain.csv --symbol SPY --spot 757.63

CSV columns: type,strike,open_interest,implied_volatility[,gamma][,volume]
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, date, time as dtime, timezone
from zoneinfo import ZoneInfo

# Contract multiplier and the 1% move convention used for dollar gamma.
# Overridden per-symbol for futures (see FUTURES_SPECS).
MULTIPLIER = 100
PCT_MOVE = 0.01

# Expiry cut for US options/futures options. A fixed UTC offset would be an hour
# out for the ~4 months a year that are EST, and on a 0DTE afternoon an hour is a
# large fraction of the remaining T — which flows straight into the expected move.
NY = ZoneInfo("America/New_York")
MIN_T_YEARS = 0.5 / (365.0 * 24.0)  # floor T at 30 minutes so 0DTE gamma stays finite

# Risk-free rate used only for the Black-76 discount (exp(-rT)). At 0DTE it is
# ~1.0 anyway; the exact value matters only beyond a few weeks. SOFR proxy.
DEFAULT_R = 0.045

# ---------------------------------------------------------------------------
# Futures specs — the three pillars that make futures GEX *not* equity GEX
# relabelled: (1) the pricing model, (2) the point value / multiplier,
# (3) the polling symbol.
# ---------------------------------------------------------------------------
# Sources: CME spec sheets + Schwab/CME tick-value tables. All tick sizes /
# multipliers validated against the 2026 CME spec (see dashboard.py for the
# same table rendered for the user). The *multiplier* here is the dollar
# value of a 1.00 point move in the futures price — what turns a pure gamma
# (deltas per point) into dollars. Tick = multiplier × tick_size.
FUTURES_SPECS: dict[str, dict] = {
    # Equity indices — the ones the request calls out plus their micros
    "ES":  {"multiplier": 50,   "tick": 0.25,   "yf": "ES=F",  "name": "E-mini S&P 500",        "exchange": "CME",   "model": "black76"},
    "NQ":  {"multiplier": 20,   "tick": 0.25,   "yf": "NQ=F",  "name": "E-mini Nasdaq-100",     "exchange": "CME",   "model": "black76"},
    "YM":  {"multiplier": 5,    "tick": 1.0,    "yf": "YM=F",  "name": "E-mini Dow ($5)",       "exchange": "CBOT",  "model": "black76"},
    "RTY": {"multiplier": 50,   "tick": 0.10,   "yf": "RTY=F", "name": "E-mini Russell 2000",   "exchange": "CME",   "model": "black76"},
    "MES": {"multiplier": 5,    "tick": 0.25,   "yf": "MES=F", "name": "Micro E-mini S&P 500",  "exchange": "CME",   "model": "black76"},
    "MNQ": {"multiplier": 2,    "tick": 0.25,   "yf": "MNQ=F", "name": "Micro E-mini Nasdaq",   "exchange": "CME",   "model": "black76"},
    "MYM": {"multiplier": 0.5,  "tick": 1.0,    "yf": "MYM=F", "name": "Micro E-mini Dow",      "exchange": "CBOT",  "model": "black76"},
    "M2K": {"multiplier": 5,    "tick": 0.10,   "yf": "M2K=F", "name": "Micro E-mini Russell",  "exchange": "CME",   "model": "black76"},
    # Metals — GC the headliner the user asked for
    "GC":  {"multiplier": 100,  "tick": 0.10,   "yf": "GC=F",  "name": "Gold",                  "exchange": "COMEX", "model": "black76"},
    "MGC": {"multiplier": 10,   "tick": 0.10,   "yf": "MGC=F", "name": "Micro Gold",            "exchange": "COMEX", "model": "black76"},
    "SI":  {"multiplier": 5000, "tick": 0.005,  "yf": "SI=F",  "name": "Silver",                "exchange": "COMEX", "model": "black76"},
    "HG":  {"multiplier": 25000,"tick": 0.0005, "yf": "HG=F",  "name": "Copper",                "exchange": "COMEX", "model": "black76"},
    "PL":  {"multiplier": 50,   "tick": 0.10,   "yf": "PL=F",  "name": "Platinum",              "exchange": "NYMEX", "model": "black76"},
    # Energy
    "CL":  {"multiplier": 1000, "tick": 0.01,   "yf": "CL=F",  "name": "WTI Crude Oil",         "exchange": "NYMEX", "model": "black76"},
    "MCL": {"multiplier": 100,  "tick": 0.01,   "yf": "MCL=F", "name": "Micro WTI Crude",       "exchange": "NYMEX", "model": "black76"},
    "NG":  {"multiplier": 10000,"tick": 0.001,  "yf": "NG=F",  "name": "Natural Gas",           "exchange": "NYMEX", "model": "black76"},
    "HO":  {"multiplier": 42000,"tick": 0.0001, "yf": "HO=F",  "name": "Heating Oil",           "exchange": "NYMEX", "model": "black76"},
    "RB":  {"multiplier": 42000,"tick": 0.0001, "yf": "RB=F",  "name": "RBOB Gasoline",         "exchange": "NYMEX", "model": "black76"},
    # Rates
    "ZB":  {"multiplier": 1000, "tick": 0.03125, "yf": "ZB=F", "name": "30Y T-Bond",             "exchange": "CBOT",  "model": "black76"},
    "ZN":  {"multiplier": 1000, "tick": 0.015625,"yf": "ZN=F", "name": "10Y T-Note",            "exchange": "CBOT",  "model": "black76"},
    "ZT":  {"multiplier": 1000, "tick": 0.0078125,"yf":"ZT=F", "name": "2Y T-Note",             "exchange": "CBOT",  "model": "black76"},
    # FX (quoted differently but still Black-76 on the forward)
    "6E":  {"multiplier": 125000,"tick": 0.00005, "yf": "6E=F","name": "Euro FX",               "exchange": "CME",   "model": "black76"},
    "6B":  {"multiplier": 62500, "tick": 0.0001,  "yf": "6B=F","name": "British Pound",         "exchange": "CME",   "model": "black76"},
    "6J":  {"multiplier": 12500000,"tick":0.0000005,"yf":"6J=F","name":"Japanese Yen",         "exchange": "CME",   "model": "black76"},
    # Crypto (CME)
    "BTC": {"multiplier": 5,    "tick": 5,      "yf": "BTC-USD","name": "Bitcoin (CME 5 BTC)","exchange": "CME",   "model": "black76"},
    "ETH": {"multiplier": 50,   "tick": 0.5,    "yf": "ETH-USD","name": "Ether (CME 50 ETH)",  "exchange": "CME",   "model": "black76"},
    # Volatility
    "VIX": {"multiplier": 1000, "tick": 0.05,   "yf": "^VIX", "name": "VIX",                   "exchange": "CBOE",  "model": "black_scholes"},
}


def _normalize_symbol(sym: str) -> str:
    """Strip common suffixes so 'ES=F', '/ES', 'ES1!' all resolve to 'ES'."""
    s = sym.strip().upper()
    # strip leading slash used by some platforms (/ES)
    if s.startswith("/"):
        s = s[1:]
    # strip yahoo suffixes
    if s.endswith("=F"):
        s = s[:-2]
    # strip TradingView continuous markers: ES1!, ES2!, NQ1! etc
    if s.endswith("!"):
        s = s[:-1]
        # remove trailing digit left after stripping !
        while s and s[-1].isdigit():
            s = s[:-1]
    # CME micro shorthands may be MES1! style — already handled
    # handle -USD crypto
    if s.endswith("-USD"):
        s = s[:-4]
    # handle ^VIX style
    if s.startswith("^"):
        s = s[1:]
    return s


def futures_spec(symbol: str) -> dict | None:
    return FUTURES_SPECS.get(_normalize_symbol(symbol))


def is_futures(symbol: str) -> bool:
    return futures_spec(symbol) is not None


def yf_symbol_for(symbol: str) -> str:
    spec = futures_spec(symbol)
    if spec:
        return spec["yf"]
    # Equities: pass through as-is; already a valid yahoo ticker
    return symbol


def multiplier_for(symbol: str, override: float | None = None) -> float:
    if override is not None:
        return float(override)
    spec = futures_spec(symbol)
    return float(spec["multiplier"]) if spec else float(MULTIPLIER)


def model_for(symbol: str, override: str | None = None) -> str:
    if override and override != "auto":
        return override
    spec = futures_spec(symbol)
    if spec:
        return spec["model"]
    return "black_scholes"


# ---------------------------------------------------------------------------
# Black-Scholes / Black-76
# ---------------------------------------------------------------------------
def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_gamma(S: float, K: float, T: float, sigma: float, r: float = 0.0, q: float = 0.0) -> float:
    """Black-Scholes gamma. Identical for calls and puts."""
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    vol_t = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / vol_t
    return math.exp(-q * T) * norm_pdf(d1) / (S * vol_t)


def black76_gamma(F: float, K: float, T: float, sigma: float, r: float = DEFAULT_R) -> float:
    """
    Black-76 gamma — options on futures/forwards.

    For a European option on a forward F (futures price), with strike K,
    time T, vol sigma, risk-free r:

        C = e^{-rT} [ F N(d1) - K N(d2) ]
        d1 = [ln(F/K) + 0.5 sigma² T] / (sigma sqrt(T))

        gamma_F = e^{-rT} * φ(d1) / (F sigma sqrt(T))

    This is exactly Black-Scholes with S=F, q=r — the clean substitution
    noted by Black (1976). The discounted delta is e^{-rT} N(d1); its
    derivative w.r.t. F is the discounted gamma above. Some desks quote
    *undiscounted* gamma (drop the e^{-rT}); at 0DTE the factor is 0.9999 and
    the difference is immaterial, but we keep it for correctness beyond a week.

    For very short T the discount is negligible; passing r=0 recovers the
    undiscounted form.
    """
    if F <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return 0.0
    vol_t = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / vol_t
    return math.exp(-r * T) * norm_pdf(d1) / (F * vol_t)


def gamma_for(
    S_or_F: float,
    K: float,
    T: float,
    sigma: float,
    model: str = "black_scholes",
    r: float = DEFAULT_R,
    q: float = 0.0,
) -> float:
    """Dispatch to the correct gamma model."""
    if model == "black76":
        return black76_gamma(S_or_F, K, T, sigma, r)
    return bs_gamma(S_or_F, K, T, sigma, r, q)


def year_fraction(expiry: date, now: datetime | None = None) -> float:
    """Time to the 16:00 ET expiry cut, in years, floored so 0DTE stays finite."""
    now = now or datetime.now(timezone.utc)
    cut = datetime.combine(expiry, dtime(16, 0), tzinfo=NY)
    seconds = (cut - now).total_seconds()
    return max(seconds / (365.0 * 24.0 * 3600.0), MIN_T_YEARS)


# ---------------------------------------------------------------------------
# Chain model
# ---------------------------------------------------------------------------
@dataclass
class Contract:
    kind: str          # "call" | "put"
    strike: float
    open_interest: float
    iv: float
    gamma: float | None = None   # supplied by broker when available
    volume: float = 0.0          # same-session traded contracts, when the feed has it


# Open interest is published once a day, after the close. On a 0DTE chain that
# makes it a photograph of yesterday: the contracts driving today's hedging were
# opened this morning and are invisible to it. Same-session volume is the other
# half of the picture — noisier, since it counts both opens and closes, but it is
# the only view of what actually traded today.
SIZE_MODES = ("oi", "max", "sum", "volume", "auto")


def contract_size(c: Contract, mode: str) -> float:
    """Resolve the size a contract contributes, per the chosen open-interest mode."""
    if mode == "volume":
        return c.volume
    if mode == "max":
        return max(c.open_interest, c.volume)
    if mode == "sum":
        return c.open_interest + c.volume
    return c.open_interest


def apply_size_mode(contracts: list[Contract], mode: str) -> list[Contract]:
    """Rewrite open_interest to the resolved size so the rest of the maths is unchanged."""
    if mode == "oi":
        return contracts
    return [
        Contract(c.kind, c.strike, contract_size(c, mode), c.iv, c.gamma, c.volume)
        for c in contracts
    ]


@dataclass
class Levels:
    symbol: str
    spot: float
    expiry: str
    net_gex: float = 0.0
    call_wall: float | None = None
    put_wall: float | None = None
    call_wall_2: float | None = None
    put_wall_2: float | None = None
    call_wall_3: float | None = None
    put_wall_3: float | None = None
    gamma_flip: float | None = None
    control_node: float | None = None
    expected_move: float | None = None
    atm_iv: float | None = None
    # Open-interest walls. Near expiry, gamma is so peaked at the money that the
    # GEX walls above collapse toward spot; these mark where the size actually
    # sits, which is often the more useful target on a 0DTE chart.
    call_wall_oi: float | None = None
    put_wall_oi: float | None = None
    size_mode: str = "oi"
    profile: list[tuple[float, float]] = field(default_factory=list)
    # Futures-aware extras — keep JSON backward-compatible (old readers ignore them)
    multiplier: float = 100
    model: str = "black_scholes"
    is_futures: bool = False
    underlying_label: str = "spot"
    point_value: float = 100  # alias for multiplier in display


# ---------------------------------------------------------------------------
# GEX math
# ---------------------------------------------------------------------------
def strike_gex(
    c: Contract,
    S: float,
    T: float,
    multiplier: float = MULTIPLIER,
    model: str = "black_scholes",
    r: float = 0.0,
) -> float:
    """
    Dollar gamma exposure for one strike, in dollars per 1% move.

    Sign convention: dealers are assumed long calls / short puts against
    customer flow, so calls contribute positive gamma and puts negative.

    For futures, `S` is the futures forward F, `model` should be ``black76``
    and `multiplier` is the CME point value ($50 for ES, $20 for NQ, $100 for
    GC …). For equities it is $100 × underlying.
    """
    g = c.gamma if c.gamma is not None else gamma_for(S, c.strike, T, c.iv, model, r)
    notional = g * c.open_interest * multiplier * S * S * PCT_MOVE
    return notional if c.kind == "call" else -notional


def build_profile(
    contracts: list[Contract],
    S: float,
    T: float,
    multiplier: float = MULTIPLIER,
    model: str = "black_scholes",
    r: float = 0.0,
) -> dict[float, float]:
    prof: dict[float, float] = {}
    for c in contracts:
        if c.open_interest <= 0:
            continue
        prof[c.strike] = prof.get(c.strike, 0.0) + strike_gex(c, S, T, multiplier, model, r)
    return prof


def total_gex_at(
    contracts: list[Contract],
    spot: float,
    T: float,
    multiplier: float = MULTIPLIER,
    model: str = "black_scholes",
    r: float = 0.0,
) -> float:
    """Total GEX if spot/forward were `spot`, re-evaluating every gamma at that level."""
    total = 0.0
    for c in contracts:
        if c.open_interest <= 0 or c.iv <= 0:
            continue
        g = gamma_for(spot, c.strike, T, c.iv, model, r)
        notional = g * c.open_interest * multiplier * spot * spot * PCT_MOVE
        total += notional if c.kind == "call" else -notional
    return total


def _scan_flip(
    contracts: list[Contract],
    S: float,
    T: float,
    width: float,
    steps: int,
    multiplier: float = MULTIPLIER,
    model: str = "black_scholes",
    r: float = 0.0,
) -> tuple[float | None, int]:
    """
    One pass of the zero-gamma scan over ±`width` around spot/forward.

    Returns the sign crossing nearest spot and how many grid points carried any
    information. Points where aggregate gamma is *exactly* zero are dropped
    rather than treated as a crossing: on a 0DTE chain every strike more than a
    few percent from spot has a gamma that underflows to 0.0 in float, so a zero
    means "no evidence here", not "the flip is here".
    """
    lo, hi = S * (1 - width), S * (1 + width)
    xs: list[float] = []
    ys: list[float] = []
    for i in range(steps + 1):
        x = lo + i * (hi - lo) / steps
        y = total_gex_at(contracts, x, T, multiplier, model, r)
        if y != 0.0:
            xs.append(x)
            ys.append(y)

    best: float | None = None
    for i in range(1, len(xs)):
        y0, y1 = ys[i - 1], ys[i]
        if (y0 < 0) != (y1 < 0):
            x0, x1 = xs[i - 1], xs[i]
            cross = x0 + (x1 - x0) * abs(y0) / (abs(y0) + abs(y1))
            # Several crossings are possible on a lumpy chain. The one that
            # governs today's hedging is the one price is actually sitting
            # against, so keep the nearest rather than the first one found.
            if best is None or abs(cross - S) < abs(best - S):
                best = cross
    return best, len(xs)


def find_gamma_flip(
    contracts: list[Contract],
    S: float,
    T: float,
    span: float = 0.10,
    steps: int = 240,
    multiplier: float = MULTIPLIER,
    model: str = "black_scholes",
    r: float = 0.0,
) -> float | None:
    """
    Zero-gamma level: the spot/forward at which aggregate dealer gamma changes sign.

    Scans a grid around spot and linearly interpolates the crossing, which is
    more faithful than summing GEX cumulatively across strikes because gamma
    itself is re-evaluated at each candidate level.

    The window narrows when the wide grid is mostly dead. Near expiry gamma
    collapses into a band a fraction of a percent wide, and a ±10% grid then
    samples almost nothing but zeros; halving until the grid has something to
    say keeps the resolution where the gamma actually lives. Returns None when
    no crossing exists rather than inventing one — a flip the data does not
    support is worse than no flip at all, because every regime read downstream
    is built on it.
    """
    width = span
    for _ in range(8):
        best, live = _scan_flip(contracts, S, T, width, steps, multiplier, model, r)
        if best is not None:
            return best
        # A grid that was well populated and still found no crossing has given a
        # real answer: there is no sign change here. Only keep narrowing while
        # most of the grid is underflowed zeros.
        if live > steps * 0.5:
            return None
        width /= 2.0
    return None


def resolve_size_mode(mode: str, expiry: date, now: datetime | None = None) -> str:
    """`auto` means: blend volume in on a 0DTE chain, trust open interest otherwise."""
    if mode != "auto":
        return mode
    today = (now or datetime.now(timezone.utc)).astimezone(NY).date()
    return "max" if expiry <= today else "oi"


def derive_levels(
    symbol: str,
    expiry: date,
    contracts: list[Contract],
    S: float,
    size_mode: str = "oi",
    now: datetime | None = None,
    multiplier: float | None = None,
    model: str | None = None,
    r: float = DEFAULT_R,
) -> Levels:
    T = year_fraction(expiry, now)
    mode = resolve_size_mode(size_mode, expiry, now)
    contracts = apply_size_mode(contracts, mode)

    # Futures auto-detection — the three things that must travel together:
    # multiplier, pricing model, and the label the dashboard/Pine show.
    spec = futures_spec(symbol)
    fut = spec is not None
    mult = multiplier_for(symbol, multiplier)
    mdl = model_for(symbol, model)

    # Allow explicit model override to flip `fut` flag if the caller insists.
    # e.g., --model black_scholes on ES forces equity treatment even though ES
    # is known to be a future. Rarely needed, but keeps the API orthogonal.
    if model is not None and model != "auto":
        mdl = model
        fut = (mdl == "black76")

    r_eff = r if mdl == "black76" else 0.0
    prof = build_profile(contracts, S, T, mult, mdl, r_eff)
    label = "forward" if fut else "spot"
    lv = Levels(
        symbol=symbol,
        spot=S,
        expiry=expiry.isoformat(),
        size_mode=mode,
        multiplier=mult,
        model=mdl,
        is_futures=fut,
        underlying_label=label,
        point_value=mult,
    )

    if not prof:
        return lv

    lv.net_gex = sum(prof.values())
    lv.profile = sorted(prof.items())

    calls_above = {k: v for k, v in prof.items() if v > 0 and k >= S}
    puts_below = {k: v for k, v in prof.items() if v < 0 and k <= S}

    # Walls: largest positive GEX above spot/forward, most negative below.
    if calls_above:
        ranked = sorted(calls_above.items(), key=lambda kv: kv[1], reverse=True)
        lv.call_wall = ranked[0][0]
        if len(ranked) > 1:
            lv.call_wall_2 = ranked[1][0]
        if len(ranked) > 2:
            lv.call_wall_3 = ranked[2][0]
    if puts_below:
        ranked = sorted(puts_below.items(), key=lambda kv: kv[1])
        lv.put_wall = ranked[0][0]
        if len(ranked) > 1:
            lv.put_wall_2 = ranked[1][0]
        if len(ranked) > 2:
            lv.put_wall_3 = ranked[2][0]

    # Control node: the single strike with the most absolute gamma — the magnet.
    lv.control_node = max(prof.items(), key=lambda kv: abs(kv[1]))[0]

    # Raw open-interest walls, unweighted by gamma.
    call_oi = {c.strike: c.open_interest for c in contracts
               if c.kind == "call" and c.strike >= S and c.open_interest > 0}
    put_oi = {c.strike: c.open_interest for c in contracts
              if c.kind == "put" and c.strike <= S and c.open_interest > 0}
    if call_oi:
        lv.call_wall_oi = max(call_oi.items(), key=lambda kv: kv[1])[0]
    if put_oi:
        lv.put_wall_oi = max(put_oi.items(), key=lambda kv: kv[1])[0]

    lv.gamma_flip = find_gamma_flip(contracts, S, T, multiplier=mult, model=mdl, r=r_eff)

    # ATM IV drives the expected move for the session. For futures the same
    # formula applies with F in place of S (the move is in futures points).
    atm = min(contracts, key=lambda c: abs(c.strike - S), default=None)
    if atm is not None and atm.iv > 0:
        band = [c.iv for c in contracts if abs(c.strike - S) <= max(S * 0.005, 0.01) and c.iv > 0]
        lv.atm_iv = sum(band) / len(band) if band else atm.iv
        lv.expected_move = S * lv.atm_iv * math.sqrt(T)

    return lv


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------
def load_yfinance(symbol: str, dte: int) -> tuple[list[Contract], float, date]:
    try:
        import yfinance as yf
    except ImportError:
        sys.exit("yfinance not installed. Run: pip install yfinance")

    yf_sym = yf_symbol_for(symbol)
    tk = yf.Ticker(yf_sym)
    expiries = tk.options
    if not expiries:
        # Helpful hint for futures: not every yahoo ticker carries an options chain.
        # ES=F options are not on yahoo; the user should fall back to --source csv
        # with a CME export (e.g., from CME QuikStrike or delayed chain).
        if is_futures(symbol):
            sys.exit(
                f"No expirations returned for {symbol} (yahoo ticker {yf_sym}). "
                f"CME futures options are not on yahoo — export the chain from "
                f"CME QuikStrike / delayed quotes and pass --source csv --symbol {symbol} --spot <F> --expiry YYYY-MM-DD"
            )
        sys.exit(f"No expirations returned for {symbol}")

    # Options expire on the New York calendar, not the calendar of whatever
    # machine this is running on. `date.today()` west of NY after 21:00 local, or
    # anywhere east of it in the morning, picks the wrong day — and "wrong day"
    # for --dte 0 means yesterday's expired chain or tomorrow's.
    today = datetime.now(timezone.utc).astimezone(NY).date()
    future = [e for e in expiries if date.fromisoformat(e) >= today]
    if not future:
        sys.exit(f"No future expirations for {symbol}")
    if dte >= len(future):
        print(f"  {symbol}: only {len(future)} expirations available, "
              f"--dte {dte} falls back to {future[-1]}", file=sys.stderr)
    expiry_str = future[min(dte, len(future) - 1)]
    expiry = date.fromisoformat(expiry_str)

    hist = tk.history(period="1d", interval="1m")
    if hist.empty:
        sys.exit(f"No price data for {symbol} (yahoo {yf_sym})")
    spot = float(hist["Close"].iloc[-1])

    chain = tk.option_chain(expiry_str)
    contracts: list[Contract] = []
    for frame, kind in ((chain.calls, "call"), (chain.puts, "put")):
        for _, row in frame.iterrows():
            oi = float(row.get("openInterest") or 0)
            iv = float(row.get("impliedVolatility") or 0)
            vol = float(row.get("volume") or 0)
            if (oi <= 0 and vol <= 0) or iv <= 0:
                continue
            contracts.append(Contract(kind, float(row["strike"]), oi, iv, None, vol))
    return contracts, spot, expiry


def load_csv(path: str, spot: float, expiry: date) -> tuple[list[Contract], float, date]:
    contracts: list[Contract] = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            kind = row["type"].strip().lower()
            kind = "call" if kind.startswith("c") else "put"
            oi = float(row.get("open_interest") or 0)
            iv = float(row.get("implied_volatility") or 0)
            vol = float(row.get("volume") or 0)
            raw_gamma = row.get("gamma")
            gamma = float(raw_gamma) if raw_gamma not in (None, "") else None
            if (oi <= 0 and vol <= 0) or (iv <= 0 and gamma is None):
                continue
            contracts.append(Contract(kind, float(row["strike"]), oi, iv, gamma, vol))
    return contracts, spot, expiry


# ---------------------------------------------------------------------------
# Emitters
# ---------------------------------------------------------------------------
def fmt(v: float | None, nd: int = 2) -> str:
    return "" if v is None else f"{round(v, nd):g}"


def pine_blob(lv: Levels) -> str:
    parts = [
        ("flip", lv.gamma_flip),
        ("cw", lv.call_wall),
        ("pw", lv.put_wall),
        ("cn", lv.control_node),
        ("cw2", lv.call_wall_2),
        ("pw2", lv.put_wall_2),
        ("cw3", lv.call_wall_3),
        ("pw3", lv.put_wall_3),
        # Raw open-interest walls. Near expiry the gamma-weighted walls collapse
        # onto the money and stop being usable targets; these keep a level out
        # where the size actually sits.
        ("cwo", lv.call_wall_oi),
        ("pwo", lv.put_wall_oi),
        ("em", lv.expected_move),
        # Spot/forward at generation. The expected move is the move *remaining*
        # from here to the close, so the band has to hang off this price —
        # anchoring it to the session open instead makes a midday regeneration
        # draw a half-day band around a full-day starting point.
        ("ref", lv.spot),
    ]
    body = ",".join(f"{k}:{fmt(v)}" for k, v in parts if v is not None)
    if lv.net_gex:
        body += f",net:{fmt(lv.net_gex / 1e6, 3)}"
    # Futures hint — Pine can display the correct tick/value if it knows.
    # Old Pine versions ignore unknown keys, so this is backward-compatible.
    if lv.is_futures:
        body += f",mult:{fmt(lv.multiplier, 2)}"
        # short model tag for debugging: 76 = Black-76, S = Black-Scholes
        body += f",mdl:{'76' if lv.model=='black76' else 'S'}"
    return body


def pine_profile(lv: Levels, top: int = 24) -> str:
    """Strongest strikes by |GEX|, in $M, sorted back into price order."""
    ranked = sorted(lv.profile, key=lambda kv: abs(kv[1]), reverse=True)[:top]
    return ",".join(f"{fmt(k)}={fmt(v / 1e6, 3)}" for k, v in sorted(ranked))


def print_report(lv: Levels) -> None:
    tag = "FUT" if lv.is_futures else "EQ "
    yf = yf_symbol_for(lv.symbol) if lv.is_futures else lv.symbol
    extra = ""
    if lv.is_futures:
        spec = futures_spec(lv.symbol)
        nm = spec["name"] if spec else "Futures"
        extra = f"  [{nm} · ${lv.multiplier:g}/pt · {lv.model} · yahoo {yf}]"
    print(f"\n═══ {lv.symbol}  {tag} {lv.underlying_label} {lv.spot:.2f}  exp {lv.expiry}  size {lv.size_mode}{extra} ═══")
    print(f"  net GEX      {lv.net_gex / 1e6:>10.2f} $M / 1%")
    if lv.gamma_flip is None:
        print("  gamma flip          —   (no sign change in the chain — regime unknown)")
    else:
        print(f"  gamma flip   {fmt(lv.gamma_flip):>10}   "
              f"({'above — negative γ' if lv.gamma_flip > lv.spot else 'below — positive γ'})")
    print(f"  call wall    {fmt(lv.call_wall):>10}   (oi wall {fmt(lv.call_wall_oi)})")
    print(f"  put wall     {fmt(lv.put_wall):>10}   (oi wall {fmt(lv.put_wall_oi)})")
    print(f"  control node {fmt(lv.control_node):>10}")
    print(f"  expected move ±{fmt(lv.expected_move):>8}  (atm iv {fmt(lv.atm_iv, 4)})  [{lv.underlying_label}]")
    if lv.is_futures:
        # Dollar-terms check — ES 20 pts ≈ $1000, GC $15 ≈ $1500, etc.
        if lv.expected_move:
            print(f"  notional EM  ~${abs(lv.expected_move * lv.multiplier):,.0f} per contract")
    print(f"\n  Pine blob:\n    {pine_blob(lv)}")
    print(f"\n  Pine profile:\n    {pine_profile(lv)}")


# ---------------------------------------------------------------------------
# Live server
# ---------------------------------------------------------------------------
def run_server(symbols: list[str], dte: int, port: int, interval: int,
               size_mode: str = "oi", multiplier: float | None = None,
               model: str | None = None, r: float = DEFAULT_R) -> None:
    """
    Serve a self-refreshing dashboard. A background thread re-pulls the chains
    on `interval`; the page polls /api/levels and repaints without a reload, so
    the levels track the session instead of freezing at whatever the open was.
    """
    import http.server
    import json as _json
    import threading
    import time

    from dashboard import render_dashboard, payload_for

    state: dict = {"levels": [], "updated": "", "error": None}
    ready = threading.Event()

    # Last good levels per symbol. One symbol failing — a thin chain, a rate
    # limit, a momentary 404 — used to discard the whole batch, so a hiccup on
    # IWM blanked SPY too. Each symbol now keeps its own last good snapshot and
    # only the ones that actually failed go stale.
    cache: dict[str, Levels] = {}

    def refresh_loop():
        while True:
            errors = []
            for sym in symbols:
                try:
                    contracts, spot, expiry = load_yfinance(sym, dte)
                    cache[sym] = derive_levels(sym, expiry, contracts, spot, size_mode,
                                               multiplier=multiplier, model=model, r=r)
                except Exception as exc:                  # keep serving stale data
                    errors.append(f"{sym}: {exc}")
            state["levels"] = [cache[s] for s in symbols if s in cache]
            state["updated"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            state["error"] = "; ".join(errors) if errors else None
            fresh = [s for s in symbols if s not in {e.split(":")[0] for e in errors}]
            print(f"[{state['updated']}] refreshed {', '.join(fresh) or '—'}"
                  + (f"  ({len(errors)} failed: {state['error']})" if errors else ""))
            if errors:
                print(f"refresh failed: {state['error']}", file=sys.stderr)
            ready.set()
            time.sleep(interval)

    threading.Thread(target=refresh_loop, daemon=True).start()
    print("fetching first snapshot…")
    ready.wait(timeout=90)
    if not state["levels"]:
        sys.exit(f"could not load any chain: {state['error']}")

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/levels"):
                body = _json.dumps({
                    "levels": payload_for(state["levels"]),
                    "updated": state["updated"],
                    "error": state["error"],
                }).encode()
                self._send(body, "application/json")
            else:
                html = render_dashboard(state["levels"], live=True,
                                        poll_ms=max(interval, 5) * 1000)
                self._send(html.encode(), "text/html; charset=utf-8")

        def log_message(self, *args):
            pass                                          # quiet; we log refreshes

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"\n  live dashboard → http://127.0.0.1:{port}")
    print(f"  refreshing every {interval}s · ctrl-c to stop\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default="SPY", help="comma separated tickers — equities (SPY, QQQ) and futures (ES, NQ, GC, CL) both accepted")
    ap.add_argument("--dte", type=int, default=0, help="0 = nearest expiry, 1 = next, ...")
    ap.add_argument("--source", choices=["yfinance", "csv"], default="yfinance")
    ap.add_argument("--csv", help="chain export when --source csv")
    ap.add_argument("--spot", type=float, help="spot/forward price when --source csv (for futures this is the futures price F)")
    ap.add_argument("--expiry", help="YYYY-MM-DD when --source csv")
    ap.add_argument("--json", help="write levels JSON here")
    ap.add_argument("--html", help="write dashboard HTML here")
    ap.add_argument("--serve", action="store_true",
                    help="run a live auto-refreshing dashboard instead of a one-shot report")
    ap.add_argument("--port", type=int, default=8787, help="port for --serve")
    ap.add_argument("--interval", type=int, default=60,
                    help="seconds between chain refreshes when serving")
    ap.add_argument("--oi-mode", choices=list(SIZE_MODES), default="auto",
                    help="how to size each contract: oi (published, one day stale), "
                         "volume (today only), max/sum of the two, or auto — max on a "
                         "0DTE chain where open interest predates the session, oi otherwise")
    ap.add_argument("--multiplier", type=float, default=None,
                    help="override the contract point value ($/point). For equities default is 100; "
                         "for ES it is 50, NQ 20, GC 100, SI 5000, CL 1000, etc. Auto-detected when omitted.")
    ap.add_argument("--model", choices=["auto", "black_scholes", "black76"], default="auto",
                    help="pricing model for gamma. 'auto' uses Black-76 for futures (ES/NQ/GC/...) and "
                         "Black-Scholes for equities. Override to force one or the other.")
    ap.add_argument("--risk-free", type=float, default=DEFAULT_R,
                    help="risk-free rate for Black-76 discount (default 0.045). Ignored for Black-Scholes.")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    model_arg = None if args.model == "auto" else args.model

    if args.serve:
        if args.source == "csv":
            sys.exit("--serve needs a live source; drop --source csv")
        run_server(symbols, args.dte, args.port, max(15, args.interval), args.oi_mode,
                   multiplier=args.multiplier, model=model_arg, r=args.risk_free)
        return

    results: list[Levels] = []

    for sym in symbols:
        if args.source == "csv":
            if not (args.csv and args.spot and args.expiry):
                sys.exit("--source csv requires --csv, --spot and --expiry")
            # One CSV is one chain. Looping it over several symbols produced N
            # identical level sets wearing different tickers, which looks like
            # real output and is worse than an error.
            if len(symbols) > 1:
                sys.exit("--source csv reads a single chain; pass one --symbols value")
            contracts, spot, expiry = load_csv(args.csv, args.spot,
                                               date.fromisoformat(args.expiry))
        else:
            contracts, spot, expiry = load_yfinance(sym, args.dte)

        lv = derive_levels(sym, expiry, contracts, spot, args.oi_mode,
                           multiplier=args.multiplier, model=model_arg, r=args.risk_free)
        results.append(lv)
        print_report(lv)

    if args.json:
        payload = [asdict(lv) for lv in results]
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"\nwrote {args.json}")

    if args.html:
        from dashboard import render_dashboard  # local module
        html = render_dashboard(results)
        with open(args.html, "w") as fh:
            fh.write(html)
        print(f"wrote {args.html}")


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    main()
