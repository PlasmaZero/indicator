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

## Confluences and confidence

Fifteen independent reads on the tape. Each casts a weighted bull / bear /
neutral vote, and **confidence** combines two separate things:

```
purity        = |bullWeight − bearWeight| / activeWeight    # how one-sided
participation = activeWeight / availableWeight              # how much has an opinion
confidence    = purity × √participation × 100
```

*Active* weight is the confluences actually voting; *available* is those with
data. The square root matters: dividing straight through by available weight
double-penalises, because neutral confluences shrink the numerator **and** votes
are magnitude-scaled. Under that formula a solid nine-confluence trend scored
only 43% and the 60% gate was effectively unreachable — a day could pass with a
single signal. Softening participation lets a real setup clear the bar while two
weak votes on their own still can't.

Roughly what the numbers mean:

| Setup | Confidence | Clears |
|---|---|---|
| 2 weak confluences | ~21% | nothing |
| 5 moderate | ~37% | Max |
| 7 moderate | ~54% | Aggressive |
| 9 strong | ~66% | Balanced |
| 12 strong | ~80% | Conservative |
| conflicted (6 up, 4 down) | ~25% | nothing — correctly suppressed |

| # | Confluence | Wt | What it reads |
|---|---|---|---|
| 1 | Trend | 12 | EMA(9/21) spread in ATR units |
| 2 | Momentum | 10 | RSI displacement from 50 |
| 3 | Relative volume | 8 | Participation confirming the bar's direction |
| 4 | Cumulative delta | 10 | Session order-flow slope |
| 5 | VWAP | 8 | Distance from the session's value anchor |
| 6 | Liquidity sweep | 12 | Failed break of a prior swing — the best single tell here |
| 7 | Displacement | 8 | Conviction of the current bar |
| 8 | Gamma-regime fit | 14 | Short gamma amplifies the move; long gamma pulls to the node |
| 9 | Control-node magnet | 10 | Direction and strength of the pin |
| 10 | Room to next level | 8 | Asymmetry of space — far ceiling, near floor favours longs |
| 11 | Opening range | 10 | Above/below the first 30 minutes |
| 12 | Expected-move band | 10 | Exhaustion at the edges of the day's priced range |
| 13 | Higher timeframe | 12 | 15m trend, read from the last *completed* HTF bar |
| 14 | Market context | 8 | SPY above/below its VWAP as a breadth proxy |
| 15 | Delta divergence | 10 | New price extreme that order flow doesn't confirm |

**Confluences with no data are excluded, not counted as abstentions.** A sweep
that didn't happen is real information and should dilute confidence; an
expected-move band that doesn't exist is not information at all. So without a
GEX blob the EM-band and gamma-fit confluences drop out of the denominator
entirely (the latter would otherwise just restate VWAP), and confidence is
computed across the 13 that can actually see something. The HUD's "9/13 agree"
counts what's live right now.

### Signal frequency

The **Signal frequency** preset moves all four gates together:

| Preset | Confidence | Agreeing | Cooldown | Min R:R |
|---|---|---|---|---|
| Conservative | 70% | 6 | 8 bars | 1.6 |
| Balanced *(default)* | 60% | 5 | 5 bars | 1.3 |
| Aggressive | 45% | 3 | 2 bars | 1.0 |
| Max | 32% | 2 | 0 bars | 0.7 |
| Custom | uses the fields below verbatim | | | |

**On opposite signal** (Targets & risk) is the other big lever, and usually the
bigger one. Only a single position runs at a time, so a signal that fires
against an open trade was previously discarded outright — on a trending day
that silently swallowed most of the session. Set to *Exit* (the default) it
closes on the opposing signal and frees the slot for the next bar.

More signals is not more edge. Max takes almost anything with a directional
lean and its hit rate will be materially worse; the R multiples in the
scorecard are the thing to watch, not the trade count. Remaining limiters are
the regime filter and the pin guard, both in their own settings groups.

A signal needs **three** things, not one:

1. Confidence ≥ your minimum (default 60%)
2. At least N separate confluences agreeing (default 5) — so no single heavy
   weight can carry a trade alone
3. Reward:risk to the first target ≥ your minimum

Confidence is shown two ways for the same number: a **1-10 score** on the trade
block and in the HUD headline, and the underlying percentage next to it. It also
maps to a grade — **A** ≥ 75, **B** ≥ 60, **C** ≥ 45, **D** below.

The score in a trade block is **frozen at entry**, so it always reflects the
reading the trade was actually taken on rather than whatever the latest bar
says. The HUD headline is live.

Set any weight to **0** to switch that confluence off.

The HUD shows the direction, confidence with a meter, the grade, how many
confluences agree, the regime, the path each way and whether real GEX is
loaded. Turn on *HUD · list active confluences* to see exactly which ones are
voting and which way.

The **gamma-regime fit** confluence is where GEX earns its keep:

- *Positive gamma* — biases toward the control node, and the regime filter
  blocks longs pressed into the call wall and shorts into the put wall.
- *Negative gamma* — biases with the prevailing move, since in short-gamma
  conditions a break tends to extend rather than revert.

A **regime filter** (on by default) additionally blocks longs within 0.4 ATR of
the call wall and shorts within 0.4 ATR of the put wall while in positive gamma
— the two setups most reliably punished by dealer suppression.

Signals only fire on **confirmed bar closes**, respect a cooldown, and skip the
first and last few bars of the session.

---

## Trade management

Every signal becomes a tracked trade drawn on the chart — entry, stop, two
targets, and the exit when it resolves.

**Targets are GEX levels, not ATR guesses.** Price travels between walls and
stalls at them, so the levels themselves are the natural exits. TP1 is the next
level in the trade's direction, TP2 the one beyond it; anything closer than
0.35 ATR is skipped so TP1 is never a few ticks away. With no chain loaded it
falls back to prior-day and overnight levels, then to a measured move.

**Stops** sit beyond the 5-bar swing or an ATR multiple, whichever is wider,
then get capped by *Max risk (ATR mult)* so a distant structural level can't
create an oversized loss.

On the chart:

| Drawing | Meaning |
|---|---|
| White line | Entry |
| Red dashed + shaded zone | Stop and risk |
| Green line + shaded zone | TP1 and reward |
| Green dotted | TP2 |
| ✕ label | Exit, with reason and R multiple |

The entry block reads:

```
▲ LONG  737.38
conf 8/10  ●●●●●●●●○○  B
stop 736.08
tp1 739.47  (1.6R)
tp2 743.48
γ+ · 9 agree · RVOL 3.9
```

Exits fire on: **STOP**, **TP2**, **BE** (stopped after breakeven move),
**REV** (a signal fired against the position), **EOD** (flattened at the close —
0DTE contracts decay to nothing overnight), and optionally **FLIP** (score
reverses against the position). At TP1 the stop
moves to breakeven by default, so the runner to TP2 is free.

One position at a time — these are intraday signals, not a portfolio.

A **session scorecard** in the bottom right tracks trades, W/L, win rate and
total R for the day, resetting each morning. It's a live read on whether the
current settings are working on this symbol, not a backtest.

**Alerts:** *ITM·GEX Buy*, *ITM·GEX Sell* and *ITM·GEX Exit*. The exit alert
carries the reason and the R multiple.

### Pin guard

Late in the session, in positive gamma, with price parked on the control node,
dealer hedging tends to keep it there — entries in that state pay the spread and
go nowhere. *Block new entries pinned to the control node* (on by default)
suppresses them.

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
GEX-by-strike profile, spot and flip marked, headline tiles and the Pine blobs
ready to copy. A sample built from the test fixture lives at
`docs/sample_dashboard.html`.

### Live mode

```bash
python3 gex_engine.py --symbols SPY,QQQ,IWM,TSLA,NVDA --dte 0 --serve
```

Opens `http://127.0.0.1:8787`. A background thread re-pulls the chains every
60s (`--interval`) and the page polls and repaints in place, so your open tab
and scroll position survive each update — the levels track the session instead
of freezing at whatever the open was. A pulsing **LIVE** badge shows the last
refresh; it goes grey if a refresh fails, and the last good data stays on
screen rather than blanking.

This is the closest thing to real-time GEX available here. **TradingView
itself cannot pull it** — Pine has no way to fetch an options chain, so the
chart levels are only as fresh as your last paste. Keep the live dashboard on
a second monitor and re-paste the blob when the levels have moved
meaningfully; on a busy 0DTE session that's usually once around midday.

What *is* genuinely real-time on the chart: the higher-timeframe trend and the
market-context confluences, both pulled live via `request.security`.

---

## How far back signals go

Two different limits, worth separating:

- **Triangles** are plots, so they appear on **every bar of loaded history** —
  as far back as your TradingView plan allows (~5k bars on Basic ≈ 64 trading
  days on 5m; more on Pro/Premium).
- **Trade drawings** (zone boxes, stop/target lines, labels) are drawing
  objects, capped at 500 each. Each trade permanently costs 2 boxes, 4 lines
  and 2 labels, and with the level and profile overhead the **line** cap binds
  first at roughly **110 trades**. Past that TradingView silently drops the
  oldest, so old trades vanish while recent ones stay.

Turn on **Lite trade drawing** to keep only the entry line and labels — about
**241 trades** of history instead of 110. Turning off *Track trades on chart*
leaves just the triangles, which are unlimited.

Everything resets per session (cumulative delta, opening range, scorecard), so
history is computed day by day exactly as it would have been live.

## How the chart looks

Levels render as a bright core line inside a stack of three translucent bands.
Pine has no real glow, so the stack fakes one — each band wider and fainter than
the one inside it — which reads as a halo rather than a hairline.

Visual weight follows importance, so the chart tells you what matters without
reading a single label:

| Tier | Levels | Treatment |
|---|---|---|
| Major | call/put wall, gamma flip, control node | full opacity, heaviest halo, 2px core |
| Secondary | CW2/PW2, CW3/PW3 | progressively dimmed |
| Context | midpoints, EM band, session open, prior close, ONH/ONL | faint |

Two more things keep it legible:

- **Distance fade** — levels far from price recede, so the ones price is
  actually working read first.
- **Label de-collision** — labels are drawn in one sorted pass and nudged apart
  when levels sit close together. Without it the flip, a midpoint and an EM band
  routinely land within cents of each other and their labels turn to mush. Pine
  can't measure the chart's vertical scale, so the gap is set in ATR terms via
  *Label spacing* — raise it if labels still collide when zoomed out.

**Levels drawn:** call/put walls 1-3, gamma flip, control node, the midpoints
between the flip and each wall, expected-move bands, session open, prior close,
prior day high/low, overnight high/low, VWAP with 1σ bands, and the per-strike
gamma profile. Everything except VWAP also feeds the target engine, so a level
you can see is a level a trade can exit at.

**Display controls:** *Level style* (Bands / Lines / Minimal), *Glow intensity*,
*Fade distant levels*, *Shade wall-to-wall range*, *Gamma midpoints*, *Label
spacing*, and **Minimal mode** — which strips it to just the flip, both walls
and the control node.

`docs/level_preview.html` is a mock chart using the same maths; open it to try
the settings without touching TradingView.

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
- **The session scorecard is not a backtest.** It counts today's signals on the
  bars you're looking at, assumes fills at the exact stop/target price, and
  ignores slippage, spread and option decay — a 5m underlying move of +1R is not
  +1R on a 0DTE contract. Treat it as a sanity check, not a track record.
- **Stop and target are assumed to fill at their exact level.** When a single
  bar spans both, the trade is scored as a loss, since intrabar order can't be
  known from 5m data.
- **The engine reads chains, it does not place orders.** No broker integration,
  by design.
- Expected move uses ATM IV × √T, which understates moves on days with event
  risk priced into the wings.

Not financial advice — it's tooling.
