"""
Self-contained HTML dashboard for the GEX engine.

render_dashboard(levels) returns a single HTML string with no external
requests — data is embedded as JSON and the chart is drawn with inline SVG.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

# Diverging pair: blue (positive gamma) ↔ red (negative gamma), neutral midpoint.
# Validated for both modes with the palette validator (all checks pass).
CSS = """
*, *::before, *::after { box-sizing: border-box; }
.viz-root {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --plane: #f9f9f7;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --muted: #898781;
  --grid: #e1e0d9;
  --baseline: #c3c2b7;
  --border: rgba(11,11,11,0.10);
  --pos: #2a78d6;
  --neg: #e34948;
  --neutral: #f0efec;
  --accent: #eda100;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .viz-root {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --plane: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --muted: #898781;
    --grid: #2c2c2a;
    --baseline: #383835;
    --border: rgba(255,255,255,0.10);
    --pos: #3987e5;
    --neg: #e66767;
    --neutral: #383835;
    --accent: #c98500;
  }
}
:root[data-theme="dark"] .viz-root {
  color-scheme: dark;
  --surface-1: #1a1a19;
  --plane: #0d0d0d;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --muted: #898781;
  --grid: #2c2c2a;
  --baseline: #383835;
  --border: rgba(255,255,255,0.10);
  --pos: #3987e5;
  --neg: #e66767;
  --neutral: #383835;
  --accent: #c98500;
}
body { margin: 0; background: var(--plane); }
.viz-root {
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--plane);
  color: var(--text-primary);
  min-height: 100vh;
  padding: 24px 20px 48px;
}
.wrap { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 1.25rem; font-weight: 600; margin: 0 0 2px; letter-spacing: -0.01em; }
.sub { color: var(--text-secondary); font-size: 0.8125rem; margin: 0 0 20px; }
.tabs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 18px; }
.tab {
  font: inherit; font-size: 0.8125rem; font-weight: 500;
  padding: 6px 14px; border-radius: 999px; cursor: pointer;
  background: var(--surface-1); color: var(--text-secondary);
  border: 1px solid var(--border);
}
.tab[aria-selected="true"] { background: var(--text-primary); color: var(--surface-1); border-color: transparent; }
.tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 18px; }
.tile { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 12px 14px; }
.tile .k { font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin-bottom: 6px; }
.tile .v { font-size: 1.375rem; font-weight: 600; line-height: 1.1; }
.tile .n { font-size: 0.75rem; color: var(--text-secondary); margin-top: 4px; }
.card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; margin-bottom: 16px; }
.card h2 { font-size: 0.9375rem; font-weight: 600; margin: 0 0 2px; }
.card .hint { font-size: 0.75rem; color: var(--text-secondary); margin: 0 0 14px; }
.legend { display: flex; gap: 16px; align-items: center; font-size: 0.75rem; color: var(--text-secondary); margin-bottom: 10px; flex-wrap: wrap; }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.swatch { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.chart-scroll { overflow-x: auto; }
svg { display: block; max-width: 100%; }
.tick { font-size: 10px; fill: var(--muted); font-variant-numeric: tabular-nums; }
.barlabel { font-size: 10px; fill: var(--text-secondary); font-variant-numeric: tabular-nums; }
.lvl-label { font-size: 10px; font-weight: 600; }
table { border-collapse: collapse; width: 100%; font-size: 0.8125rem; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid var(--grid); }
th:first-child, td:first-child { text-align: left; }
th { color: var(--muted); font-weight: 500; font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.05em; }
.blob { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.75rem;
  background: var(--plane); border: 1px solid var(--border); border-radius: 8px;
  padding: 10px 12px; word-break: break-all; color: var(--text-primary); margin-bottom: 10px; }
.blob .k { color: var(--muted); display: block; margin-bottom: 4px; font-family: system-ui, sans-serif;
  font-size: 0.6875rem; text-transform: uppercase; letter-spacing: 0.05em; }
.toggle { font: inherit; font-size: 0.75rem; color: var(--text-secondary); background: none;
  border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px; cursor: pointer; }
#tip { position: fixed; pointer-events: none; opacity: 0; transition: opacity .1s;
  background: var(--text-primary); color: var(--surface-1); font-size: 0.75rem;
  padding: 7px 10px; border-radius: 7px; z-index: 20; white-space: nowrap; font-variant-numeric: tabular-nums; }
"""

JS = r"""
const DATA = __DATA__;
const $ = (s, r) => (r || document).querySelector(s);
const money = v => {
  const a = Math.abs(v);
  if (a >= 1e9) return (v/1e9).toFixed(2) + 'B';
  if (a >= 1e6) return (v/1e6).toFixed(2) + 'M';
  if (a >= 1e3) return (v/1e3).toFixed(0) + 'K';
  return v.toFixed(0);
};
const px = n => (Math.round(n*100)/100);

// Bar with rounded corners on the data end only, anchored to the zero baseline.
function barPath(x0, x1, y, h, r) {
  const w = Math.abs(x1 - x0);
  const rr = Math.max(0, Math.min(r, w, h/2));
  if (w < 0.5) return '';
  if (x1 >= x0) {
    return `M${px(x0)},${px(y)} H${px(x1-rr)} Q${px(x1)},${px(y)} ${px(x1)},${px(y+rr)}`
         + ` V${px(y+h-rr)} Q${px(x1)},${px(y+h)} ${px(x1-rr)},${px(y+h)} H${px(x0)} Z`;
  }
  return `M${px(x0)},${px(y)} H${px(x1+rr)} Q${px(x1)},${px(y)} ${px(x1)},${px(y+rr)}`
       + ` V${px(y+h-rr)} Q${px(x1)},${px(y+h)} ${px(x1+rr)},${px(y+h)} H${px(x0)} Z`;
}

const tip = document.createElement('div');
tip.id = 'tip';
document.body.appendChild(tip);
function showTip(e, html) {
  tip.innerHTML = html;
  tip.style.opacity = 1;
  const pad = 14;
  let x = e.clientX + pad, y = e.clientY + pad;
  const r = tip.getBoundingClientRect();
  if (x + r.width > innerWidth - 8) x = e.clientX - r.width - pad;
  if (y + r.height > innerHeight - 8) y = e.clientY - r.height - pad;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
}
const hideTip = () => { tip.style.opacity = 0; };

function chart(d) {
  const rows = d.profile.slice().sort((a,b) => b[0] - a[0]);   // high strike at top
  if (!rows.length) return '<p class="hint">No open interest in this chain.</p>';

  const rowH = 20, gap = 2, padT = 26, padB = 30, padL = 62, padR = 78;
  const W = 900, plotW = W - padL - padR;
  const H = padT + rows.length * rowH + padB;
  const maxAbs = Math.max(...rows.map(r => Math.abs(r[1]))) || 1;
  const cx = padL + plotW / 2;
  const scale = v => (v / maxAbs) * (plotW / 2 - 8);
  const yOf = i => padT + i * rowH;

  let s = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img"
      aria-label="Gamma exposure by strike">`;

  // zero baseline
  s += `<line x1="${cx}" y1="${padT-8}" x2="${cx}" y2="${H-padB+4}"
        stroke="var(--baseline)" stroke-width="1"/>`;

  // x ticks
  [-1, -0.5, 0.5, 1].forEach(f => {
    const x = cx + f * (plotW/2 - 8);
    s += `<line x1="${px(x)}" y1="${padT-8}" x2="${px(x)}" y2="${H-padB+4}"
          stroke="var(--grid)" stroke-width="1"/>`;
    s += `<text class="tick" x="${px(x)}" y="${H-padB+18}" text-anchor="middle">${money(f*maxAbs)}</text>`;
  });
  s += `<text class="tick" x="${cx}" y="${H-padB+18}" text-anchor="middle">0</text>`;

  // level rules
  const strikes = rows.map(r => r[0]);
  const lo = Math.min(...strikes), hi = Math.max(...strikes);
  const yForPrice = p => {
    if (hi === lo) return padT;
    // map price onto the row grid by interpolating between strike positions
    const t = (hi - p) / (hi - lo);
    return padT + t * (rows.length * rowH);
  };
  // Spot and the gamma flip often sit within a point of each other, so lay the
  // rules at their true y but push the labels apart to keep both readable.
  const marks = [
    [d.spot, 'var(--text-primary)', 'SPOT'],
    [d.gamma_flip, 'var(--accent)', 'FLIP'],
  ].filter(m => m[0] != null)
   .map(m => ({ v: m[0], col: m[1], lab: m[2], y: yForPrice(m[0]) }))
   .sort((a, b) => a.y - b.y);

  const MIN_GAP = 13;
  marks.forEach((m, i) => {
    m.ly = m.y;
    if (i > 0 && m.ly - marks[i-1].ly < MIN_GAP) m.ly = marks[i-1].ly + MIN_GAP;
  });
  marks.forEach(m => {
    s += `<line x1="${padL-8}" y1="${px(m.y)}" x2="${W-padR+8}" y2="${px(m.y)}"
          stroke="${m.col}" stroke-width="1" stroke-dasharray="4 3" opacity="0.9"/>`;
    // leader from the rule to a displaced label
    if (Math.abs(m.ly - m.y) > 1) {
      s += `<line x1="${W-padR+8}" y1="${px(m.y)}" x2="${W-padR+12}" y2="${px(m.ly)}"
            stroke="${m.col}" stroke-width="1" opacity="0.6"/>`;
    }
    s += `<text class="lvl-label" x="${W-padR+14}" y="${px(m.ly)+3}" fill="${m.col}">${m.lab} ${m.v.toFixed(2)}</text>`;
  });

  // bars
  rows.forEach((r, i) => {
    const [k, v] = r;
    const y = yOf(i) + gap/2, h = rowH - gap;
    const x1 = cx + scale(v);
    const isNode = d.control_node != null && Math.abs(k - d.control_node) < 1e-9;
    const isWall = (d.call_wall != null && Math.abs(k-d.call_wall) < 1e-9)
                || (d.put_wall  != null && Math.abs(k-d.put_wall)  < 1e-9);
    const fill = v >= 0 ? 'var(--pos)' : 'var(--neg)';
    const p = barPath(cx, x1, y, h, 4);
    if (p) {
      s += `<path d="${p}" fill="${fill}" data-k="${k}" data-v="${v}"`
        + (isNode ? ` stroke="var(--text-primary)" stroke-width="1.5"` : '')
        + (isWall && !isNode ? ` stroke="var(--text-secondary)" stroke-width="1"` : '')
        + `><title></title></path>`;
    }
    // strike label
    s += `<text class="tick" x="${padL-12}" y="${px(y+h/2+3.5)}" text-anchor="end"`
       + (isNode ? ' font-weight="700" fill="var(--text-primary)"' : '') + `>${k}</text>`;
    // selective direct labels: only the strongest strikes
    if (Math.abs(v) / maxAbs > 0.35) {
      const anchor = v >= 0 ? 'start' : 'end';
      const lx = x1 + (v >= 0 ? 6 : -6);
      s += `<text class="barlabel" x="${px(lx)}" y="${px(y+h/2+3.5)}" text-anchor="${anchor}">${money(v)}</text>`;
    }
  });

  s += `</svg>`;
  return s;
}

function table(d) {
  const rows = d.profile.slice().sort((a,b) => b[0]-a[0]);
  let s = '<table><thead><tr><th>Strike</th><th>GEX ($/1%)</th><th>Role</th></tr></thead><tbody>';
  rows.forEach(([k,v]) => {
    const roles = [];
    if (d.control_node != null && Math.abs(k-d.control_node)<1e-9) roles.push('control node');
    if (d.call_wall != null && Math.abs(k-d.call_wall)<1e-9) roles.push('call wall');
    if (d.put_wall != null && Math.abs(k-d.put_wall)<1e-9) roles.push('put wall');
    s += `<tr><td>${k}</td><td>${money(v)}</td><td>${roles.join(', ') || '—'}</td></tr>`;
  });
  return s + '</tbody></table>';
}

function render(sym) {
  const d = DATA.find(x => x.symbol === sym);
  document.querySelectorAll('.tab').forEach(t =>
    t.setAttribute('aria-selected', String(t.dataset.sym === sym)));

  const posG = d.gamma_flip != null && d.spot > d.gamma_flip;
  const regime = d.gamma_flip == null ? 'Unknown' : posG ? 'Positive' : 'Negative';
  const regimeNote = d.gamma_flip == null ? 'no flip found in range'
    : posG ? 'γ — dealers dampen moves, fade extremes and expect pinning'
           : 'γ — dealers amplify moves, favour continuation';

  $('#tiles').innerHTML = `
    <div class="tile"><div class="k">Spot</div><div class="v">${d.spot.toFixed(2)}</div>
      <div class="n">exp ${d.expiry}</div></div>
    <div class="tile"><div class="k">Net GEX</div><div class="v">${money(d.net_gex)}</div>
      <div class="n">per 1% move</div></div>
    <div class="tile"><div class="k">Regime</div><div class="v">${regime}</div>
      <div class="n">${regimeNote}</div></div>
    <div class="tile"><div class="k">Gamma flip</div><div class="v">${d.gamma_flip == null ? '—' : d.gamma_flip.toFixed(2)}</div>
      <div class="n">zero-gamma level</div></div>
    <div class="tile"><div class="k">Expected move</div><div class="v">${d.expected_move == null ? '—' : '±' + d.expected_move.toFixed(2)}</div>
      <div class="n">atm iv ${d.atm_iv == null ? '—' : (d.atm_iv*100).toFixed(1) + '%'}</div></div>
    <div class="tile"><div class="k">Control node</div><div class="v">${d.control_node == null ? '—' : d.control_node.toFixed(2)}</div>
      <div class="n">largest |gamma| strike</div></div>
    <div class="tile"><div class="k">GEX walls</div><div class="v">${d.put_wall == null ? '—' : d.put_wall.toFixed(0)} / ${d.call_wall == null ? '—' : d.call_wall.toFixed(0)}</div>
      <div class="n">gamma-weighted put / call</div></div>
    <div class="tile"><div class="k">OI walls</div><div class="v">${d.put_wall_oi == null ? '—' : d.put_wall_oi.toFixed(0)} / ${d.call_wall_oi == null ? '—' : d.call_wall_oi.toFixed(0)}</div>
      <div class="n">where the size sits</div></div>`;

  $('#chart').innerHTML = chart(d);
  $('#table').innerHTML = table(d);
  $('#blobs').innerHTML =
      `<div class="blob"><span class="k">GEX blob → Pine "GEX blob"</span>${d.pine_blob}</div>`
    + `<div class="blob"><span class="k">Strike profile → Pine "Strike profile"</span>${d.pine_profile}</div>`;

  $('#chart').querySelectorAll('path[data-k]').forEach(p => {
    p.addEventListener('mousemove', e => {
      const v = parseFloat(p.dataset.v);
      showTip(e, `<b>${p.dataset.k}</b> · ${money(v)} $/1%<br>`
        + (v >= 0 ? 'positive gamma — supply above' : 'negative gamma — support below'));
    });
    p.addEventListener('mouseleave', hideTip);
  });
}

document.querySelectorAll('.tab').forEach(t =>
  t.addEventListener('click', () => render(t.dataset.sym)));
$('#tableToggle').addEventListener('click', () => {
  const w = $('#tableWrap');
  const open = w.hasAttribute('hidden');
  if (open) w.removeAttribute('hidden'); else w.setAttribute('hidden', '');
  $('#tableToggle').textContent = open ? 'Hide data table' : 'Show data table';
});
render(DATA[0].symbol);
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GEX levels — %(symbols)s</title>
<style>%(css)s</style>
</head>
<body>
<div class="viz-root"><div class="wrap">
  <h1>Dealer gamma exposure</h1>
  <p class="sub">Generated %(stamp)s · levels feed the Pine indicator</p>
  <div class="tabs" role="tablist">%(tabs)s</div>
  <div class="tiles" id="tiles"></div>

  <div class="card">
    <h2>GEX by strike</h2>
    <p class="hint">Positive bars are strikes where dealer hedging suppresses movement;
       negative bars are where it accelerates. The largest bar is the magnet.</p>
    <div class="legend">
      <span><i class="swatch" style="background:var(--pos)"></i>Positive gamma</span>
      <span><i class="swatch" style="background:var(--neg)"></i>Negative gamma</span>
      <span><i class="swatch" style="background:var(--text-primary)"></i>Spot</span>
      <span><i class="swatch" style="background:var(--accent)"></i>Gamma flip</span>
    </div>
    <div class="chart-scroll" id="chart"></div>
  </div>

  <div class="card">
    <h2>Pine inputs</h2>
    <p class="hint">Paste into the matching fields of the TradingView indicator.</p>
    <div id="blobs"></div>
  </div>

  <div class="card">
    <button class="toggle" id="tableToggle">Show data table</button>
    <div id="tableWrap" hidden><div style="margin-top:14px" id="table"></div></div>
  </div>
</div></div>
<script>%(js)s</script>
</body>
</html>
"""


def render_dashboard(levels_list) -> str:
    from gex_engine import pine_blob, pine_profile

    payload = []
    for lv in levels_list:
        d = asdict(lv)
        d["profile"] = [[k, v] for k, v in lv.profile]
        d["pine_blob"] = pine_blob(lv)
        d["pine_profile"] = pine_profile(lv)
        payload.append(d)

    tabs = "".join(
        f'<button class="tab" role="tab" data-sym="{d["symbol"]}" '
        f'aria-selected="false">{d["symbol"]}</button>'
        for d in payload
    )
    js = JS.replace("__DATA__", json.dumps(payload))
    return PAGE % {
        "symbols": ", ".join(d["symbol"] for d in payload),
        "css": CSS,
        "js": js,
        "tabs": tabs,
        "stamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }
