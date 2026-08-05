#!/usr/bin/env python3
"""
GEX engine — turn an options chain into dealer gamma-exposure levels.

Computes gamma exposure per strike, locates the call wall, put wall, gamma flip
(zero-gamma / HVL) and control node, then emits:

  * a JSON file of levels
  * a paste-ready blob for the Pine indicator
  * a self-contained HTML dashboard

Usage
-----
    # live chain via yfinance (pip install yfinance)
    python3 gex_engine.py --symbols SPY,QQQ,IWM,TSLA,NVDA --dte 0

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
MULTIPLIER = 100
PCT_MOVE = 0.01

# Expiry cut for US equity options. A fixed UTC offset would be an hour out for
# the ~4 months a year that are EST, and on a 0DTE afternoon an hour is a large
# fraction of the remaining T — which flows straight into the expected move.
NY = ZoneInfo("America/New_York")
MIN_T_YEARS = 0.5 / (365.0 * 24.0)  # floor T at 30 minutes so 0DTE gamma stays finite


# ---------------------------------------------------------------------------
# Black-Scholes
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


# ---------------------------------------------------------------------------
# GEX math
# ---------------------------------------------------------------------------
def strike_gex(c: Contract, S: float, T: float) -> float:
    """
    Dollar gamma exposure for one strike, in dollars per 1% move.

    Sign convention: dealers are assumed long calls / short puts against
    customer flow, so calls contribute positive gamma and puts negative.
    """
    g = c.gamma if c.gamma is not None else bs_gamma(S, c.strike, T, c.iv)
    notional = g * c.open_interest * MULTIPLIER * S * S * PCT_MOVE
    return notional if c.kind == "call" else -notional


def build_profile(contracts: list[Contract], S: float, T: float) -> dict[float, float]:
    prof: dict[float, float] = {}
    for c in contracts:
        if c.open_interest <= 0:
            continue
        prof[c.strike] = prof.get(c.strike, 0.0) + strike_gex(c, S, T)
    return prof


def total_gex_at(contracts: list[Contract], spot: float, T: float) -> float:
    """Total GEX if spot were `spot`, re-evaluating every gamma at that level."""
    total = 0.0
    for c in contracts:
        if c.open_interest <= 0 or c.iv <= 0:
            continue
        g = bs_gamma(spot, c.strike, T, c.iv)
        notional = g * c.open_interest * MULTIPLIER * spot * spot * PCT_MOVE
        total += notional if c.kind == "call" else -notional
    return total


def _scan_flip(contracts: list[Contract], S: float, T: float,
               width: float, steps: int) -> tuple[float | None, int]:
    """
    One pass of the zero-gamma scan over ±`width` around spot.

    Returns the sign crossing nearest spot and how many grid points carried any
    information. Points where aggregate gamma is *exactly* zero are dropped
    rather than treated as a crossing: on a 0DTE chain every strike more than a
    few percent from spot has a gamma that underflows to 0.0 in float, so a zero
    means "no evidence here", not "the flip is here".
    """
    lo, hi = S * (1 - width), S * (1 + width)
    step = (hi - lo) / steps
    xs: list[float] = []
    ys: list[float] = []
    for i in range(steps + 1):
        x = lo + i * step
        y = total_gex_at(contracts, x, T)
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


def find_gamma_flip(contracts: list[Contract], S: float, T: float,
                    span: float = 0.10, steps: int = 240) -> float | None:
    """
    Zero-gamma level: the spot at which aggregate dealer gamma changes sign.

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
        best, live = _scan_flip(contracts, S, T, width, steps)
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


def derive_levels(symbol: str, expiry: date, contracts: list[Contract], S: float,
                  size_mode: str = "oi", now: datetime | None = None) -> Levels:
    T = year_fraction(expiry, now)
    mode = resolve_size_mode(size_mode, expiry, now)
    contracts = apply_size_mode(contracts, mode)
    prof = build_profile(contracts, S, T)
    lv = Levels(symbol=symbol, spot=S, expiry=expiry.isoformat(), size_mode=mode)

    if not prof:
        return lv

    lv.net_gex = sum(prof.values())
    lv.profile = sorted(prof.items())

    calls_above = {k: v for k, v in prof.items() if v > 0 and k >= S}
    puts_below = {k: v for k, v in prof.items() if v < 0 and k <= S}

    # Walls: largest positive GEX above spot, most negative below.
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

    lv.gamma_flip = find_gamma_flip(contracts, S, T)

    # ATM IV drives the expected move for the session.
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

    tk = yf.Ticker(symbol)
    expiries = tk.options
    if not expiries:
        sys.exit(f"No expirations returned for {symbol}")

    today = date.today()
    future = [e for e in expiries if date.fromisoformat(e) >= today]
    if not future:
        sys.exit(f"No future expirations for {symbol}")
    expiry_str = future[min(dte, len(future) - 1)]
    expiry = date.fromisoformat(expiry_str)

    hist = tk.history(period="1d", interval="1m")
    if hist.empty:
        sys.exit(f"No price data for {symbol}")
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
        # Spot at generation. The expected move is the move *remaining* from
        # here to the close, so the band has to hang off this price — anchoring
        # it to the session open instead makes a midday regeneration draw a
        # half-day band around a full-day starting point.
        ("ref", lv.spot),
    ]
    body = ",".join(f"{k}:{fmt(v)}" for k, v in parts if v is not None)
    if lv.net_gex:
        body += f",net:{fmt(lv.net_gex / 1e6, 3)}"
    return body


def pine_profile(lv: Levels, top: int = 24) -> str:
    """Strongest strikes by |GEX|, in $M, sorted back into price order."""
    ranked = sorted(lv.profile, key=lambda kv: abs(kv[1]), reverse=True)[:top]
    return ",".join(f"{fmt(k)}={fmt(v / 1e6, 3)}" for k, v in sorted(ranked))


def print_report(lv: Levels) -> None:
    print(f"\n═══ {lv.symbol}  spot {lv.spot:.2f}  exp {lv.expiry}  size {lv.size_mode} ═══")
    print(f"  net GEX      {lv.net_gex / 1e6:>10.2f} $M / 1%")
    if lv.gamma_flip is None:
        print("  gamma flip          —   (no sign change in the chain — regime unknown)")
    else:
        print(f"  gamma flip   {fmt(lv.gamma_flip):>10}   "
              f"({'above spot — negative γ' if lv.gamma_flip > lv.spot else 'below spot — positive γ'})")
    print(f"  call wall    {fmt(lv.call_wall):>10}   (oi wall {fmt(lv.call_wall_oi)})")
    print(f"  put wall     {fmt(lv.put_wall):>10}   (oi wall {fmt(lv.put_wall_oi)})")
    print(f"  control node {fmt(lv.control_node):>10}")
    print(f"  expected move ±{fmt(lv.expected_move):>8}  (atm iv {fmt(lv.atm_iv, 4)})")
    print(f"\n  Pine blob:\n    {pine_blob(lv)}")
    print(f"\n  Pine profile:\n    {pine_profile(lv)}")


# ---------------------------------------------------------------------------
# Live server
# ---------------------------------------------------------------------------
def run_server(symbols: list[str], dte: int, port: int, interval: int,
               size_mode: str = "oi") -> None:
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

    def refresh_loop():
        while True:
            try:
                out = []
                for sym in symbols:
                    contracts, spot, expiry = load_yfinance(sym, dte)
                    out.append(derive_levels(sym, expiry, contracts, spot, size_mode))
                state["levels"] = out
                state["updated"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                state["error"] = None
                print(f"[{state['updated']}] refreshed {', '.join(symbols)}")
            except Exception as exc:                      # keep serving stale data
                state["error"] = str(exc)
                print(f"refresh failed: {exc}", file=sys.stderr)
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
    ap.add_argument("--symbols", default="SPY", help="comma separated tickers")
    ap.add_argument("--dte", type=int, default=0, help="0 = nearest expiry, 1 = next, ...")
    ap.add_argument("--source", choices=["yfinance", "csv"], default="yfinance")
    ap.add_argument("--csv", help="chain export when --source csv")
    ap.add_argument("--spot", type=float, help="spot price when --source csv")
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
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if args.serve:
        if args.source == "csv":
            sys.exit("--serve needs a live source; drop --source csv")
        run_server(symbols, args.dte, args.port, max(15, args.interval), args.oi_mode)
        return

    results: list[Levels] = []

    for sym in symbols:
        if args.source == "csv":
            if not (args.csv and args.spot and args.expiry):
                sys.exit("--source csv requires --csv, --spot and --expiry")
            contracts, spot, expiry = load_csv(args.csv, args.spot,
                                               date.fromisoformat(args.expiry))
        else:
            contracts, spot, expiry = load_yfinance(sym, args.dte)

        lv = derive_levels(sym, expiry, contracts, spot, args.oi_mode)
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
