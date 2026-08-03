# GEX Levels + 0DTE Signal Engine

Dealer gamma-exposure levels on the chart, plus a 5-minute buy/sell engine tuned
for 0DTE and 1DTE trading on SPY, QQQ, IWM, TSLA and NVDA.

Three pieces:

| Piece | What it does |
|---|---|
| `pine/itm_gex_levels.pine` | TradingView indicator — draws the levels, reads the gamma regime, fires signals with targets and alerts |
| `tools/gex_engine.py` | Turns an options chain into levels and a paste-ready blob for the indicator |
| `tools/dashboard.py` | Renders the GEX-by-strike panel as a self-contained HTML page |

---

## The idea

Market makers hold the other side of the options that retail and institutions
buy. To stay delta-neutral they hedge in the underlying, and **the direction of
that hedging flips depending on where price sits relative to the "gamma flip"
level**:

- **Above the flip — positive gamma.** Dealers sell rallies and buy dips. Range
  compresses, price gets pinned toward the largest gamma strike. *Fade the
  edges; don't chase breakouts.*
- **Below the flip — negative gamma.** Dealers sell weakness and buy strength.
  Hedging amplifies the move. *Trade continuation; breakouts extend.*

That single distinction is what the indicator uses to decide whether a setup is
a fade or a breakout — the same price action means opposite things in the two
regimes. The **control node** (largest |GEX| strike) is the magnet that answers
"where is price looking to go", and the **walls** are where it's likely to stall.

---

## Daily workflow

**1. Generate levels** (once before the open, and optionally at midday):

```bash
cd tools
pip install yfinance                      # only needed for the live chain
python3 gex_engine.py --symbols SPY,QQQ,IWM,TSLA,NVDA --dte 0 \
    --json levels.json --html dashboard.html
```

`--dte 0` is the nearest expiry (0DTE), `--dte 1` the next (1DTE).

Output per symbol:

```
═══ SPY  spot 757.63  exp 2026-08-04 ═══
  net GEX          352.43 $M / 1%
  gamma flip       756.74   (below spot — positive γ)
  call wall           760   (oi wall 765)
  put wall            750   (oi wall 750)
  control node        760
  expected move ±    4.43  (atm iv 0.1120)

  Pine blob:
    flip:756.74,cw:760,pw:750,cn:760,cw2:765,pw2:755,em:4.43,net:352.427
```

**2. Paste into TradingView.** Add `pine/itm_gex_levels.pine` to a 5-minute
chart, then paste the **Pine blob** into the *GEX blob* field and the **strike
profile** into *Strike profile*. Save an indicator template per symbol so each
chart keeps its own levels.

**3. Trade the signals.** Triangles mark entries; each carries a label with
target, stop, reward:risk and the active regime. Set a TradingView alert on
*ITM·GEX Buy* / *ITM·GEX Sell* — the alert message includes all of it.

No chain data? Leave the blob empty. The indicator falls back to auto levels
(prior day high/low, overnight high/low, session VWAP as the flip proxy, and a
volume-profile point of control as the magnet) and keeps working on any symbol.

---

## How signals are scored

Four components, each normalised to −1…+1, combined with configurable weights.
A signal fires when the composite crosses the threshold **and** the reward:risk
to the next level clears the minimum.

| Component | Default weight | What it measures |
|---|---|---|
| **Momentum** | 30 | EMA(9/21) spread in ATR units, blended with RSI displacement |
| **Volume** | 25 | Session cumulative delta slope, scaled by relative volume |
| **Liquidity** | 25 | Failed sweeps of prior swings, bar displacement, VWAP stretch |
| **Levels** | 20 | Behaviour into the nearest wall + pull toward the control node |

The **levels** component is regime-aware and is where GEX earns its keep:

- *Positive gamma* — scores the pull toward the control node, penalises longs
  pressed into the call wall and shorts pressed into the put wall.
- *Negative gamma* — rewards acceptance **through** a level, since in short-gamma
  conditions a break tends to extend rather than revert.

A **regime filter** (on by default) additionally blocks longs within 0.4 ATR of
the call wall and shorts within 0.4 ATR of the put wall while in positive gamma
— the two setups most reliably punished by dealer suppression.

Targets are the next level in the trade's direction; stops are the wider of a
5-bar swing and an ATR multiple. Signals only fire on **confirmed bar closes**,
respect a cooldown, and skip the first and last few bars of the session.

---

## GEX walls vs OI walls

The engine reports both, and near expiry they often disagree:

- **GEX walls** are gamma-weighted — what dealers actually hedge. On 0DTE gamma
  is so peaked at the money that these collapse toward spot.
- **OI walls** are raw open interest — where the size sits, often a better
  target further out.

In the bundled test fixture the 765 strike carries 30k open interest against
760's 12k, yet 760 is the GEX wall because its gamma is several times larger.
Both are shown on the dashboard; the blob ships the GEX walls, and you can paste
an OI wall into `cw2`/`pw2` if you want it drawn too.

---

## The dashboard

`--html dashboard.html` writes a standalone page (no external requests) with the
GEX-by-strike profile, spot and flip marked, stat tiles for every level, and the
Pine blobs ready to copy. A sample built from the test fixture lives at
`docs/sample_dashboard.html`.

---

## Bringing your own chain

`yfinance` gives open interest and IV; the engine computes Black-Scholes gamma
itself, so any chain export works:

```bash
python3 gex_engine.py --source csv --csv chain.csv \
    --symbols SPY --spot 757.63 --expiry 2026-08-04
```

CSV columns: `type,strike,open_interest,implied_volatility[,gamma]`. Supply a
`gamma` column and it's used directly instead of being computed.

---

## Tests

```bash
cd tools && python3 test_gex_engine.py
```

33 checks covering the gamma math, sign conventions, level derivation, blob
round-tripping, edge cases and dashboard rendering. The gamma implementation is
checked against live broker greeks (SPY 758C, 1DTE) and agrees to **1.5%** — the
residual is the broker's rate and dividend assumptions.

---

## Assumptions and limits

Worth knowing before you risk money on it:

- **Dealer positioning is assumed, not observed.** The standard convention here
  — dealers long calls, short puts — is an approximation. When it's wrong (large
  institutional call selling, for instance) the sign of the whole profile is
  wrong. GEX is a map of *likely* hedging pressure, not a certainty.
- **Levels are a snapshot.** Open interest updates once daily; intraday 0DTE
  volume can reshape the real profile substantially by the afternoon.
  Regenerate at midday on active days.
- **The signal weights are untested defaults**, chosen to be reasonable rather
  than optimised. Nothing here is backtested — Pine indicators can't be
  backtested directly; port the logic to a `strategy()` script if you want
  performance numbers before trading it.
- **The engine reads chains, it does not place orders.** No broker integration,
  by design.
- Expected move uses ATM IV × √T, which understates moves on days with event
  risk priced into the wings.

Not financial advice — it's tooling.
