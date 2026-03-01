"""
server.py — Async aiohttp dashboard server.

Serves a real-time trading terminal UI at http://localhost:POLY_UI_PORT (default 7331).

Endpoints:
  GET /           — HTML dashboard
  GET /api/state  — JSON snapshot of AgentState
  GET /api/stream — SSE stream of new log lines
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from aiohttp import web

from .state import STATE

log = logging.getLogger(__name__)

UI_PORT = int(os.environ.get("POLY_UI_PORT", "7331"))

# ── Dashboard HTML ─────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TradingFans — Live Terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg:        #020202;
  --bg2:       #060606;
  --surface:   #0a0a0a;
  --surface2:  #0f0f0f;
  --border:    #161616;
  --border2:   #222222;
  --red:       #ff0028;
  --red2:      #cc0020;
  --red-dim:   #3d0009;
  --red-hi:    #ff3355;
  --red-glow:  rgba(255,0,40,0.10);
  --red-glow2: rgba(255,0,40,0.04);
  --white:     #eeeeee;
  --white2:    #aaaaaa;
  --dim:       #383838;
  --dim2:      #242424;
  --muted:     #111111;
  --orange:    #ff6a00;
  --font: 'JetBrains Mono', 'Courier New', monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  background: var(--bg);
  color: var(--white);
  font-family: var(--font);
  font-size: 12px;
  min-height: 100vh;
  overflow-x: hidden;
}

/* Dot-grid background */
body::before {
  content: '';
  position: fixed; inset: 0;
  background-image: radial-gradient(circle, #181818 1px, transparent 1px);
  background-size: 24px 24px;
  pointer-events: none; opacity: 0.55; z-index: 0;
}

#app { position: relative; z-index: 1; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

/* ══════════════════════════════════════════
   HEADER
══════════════════════════════════════════ */
#hdr {
  display: flex; align-items: center; justify-content: space-between;
  height: 46px; padding: 0 20px; flex-shrink: 0;
  background: var(--bg2);
  border-bottom: 1px solid var(--border2);
  position: relative;
}
#hdr::after {
  content: ''; position: absolute; bottom: 0; left: 0; right: 0; height: 1px;
  background: linear-gradient(90deg, transparent 0%, var(--red) 30%, var(--red) 70%, transparent 100%);
  opacity: 0.35;
}

#logo {
  display: flex; align-items: center; gap: 10px;
  font-size: 15px; font-weight: 700; letter-spacing: 0.22em; color: var(--white);
}
#logo-dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--red);
  box-shadow: 0 0 6px var(--red), 0 0 14px rgba(255,0,40,0.4);
  animation: throb 2s ease-in-out infinite;
}
@keyframes throb { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.5;transform:scale(0.85)} }

#hdr-mid { display: flex; align-items: center; gap: 22px; font-size: 10px; color: var(--dim); letter-spacing: 0.1em; }
#hdr-mid .kv { display: flex; gap: 5px; }
#hdr-mid .kv b { color: var(--white2); font-weight: 500; }

#hdr-right { display: flex; align-items: center; gap: 14px; }
.badge { padding: 3px 9px; border-radius: 2px; font-size: 9px; font-weight: 700; letter-spacing: 0.14em; text-transform: uppercase; }
.badge-dry  { background: var(--red-dim); color: var(--red-hi); border: 1px solid #6a0015; }
.badge-live { background: #001a00; color: #00e676; border: 1px solid #00803a; box-shadow: 0 0 8px rgba(0,230,118,0.15); }

#uptime { color: var(--dim); font-size: 11px; letter-spacing: 0.08em; font-weight: 300; }
#conn { display: flex; align-items: center; gap: 5px; font-size: 10px; color: var(--dim); }
#cdot { width: 5px; height: 5px; border-radius: 50%; background: var(--dim); }
.cdot-ok  { background: var(--white2) !important; box-shadow: 0 0 5px rgba(255,255,255,0.3); }
.cdot-err { background: var(--red) !important; animation: throb 1s infinite; }

/* ══════════════════════════════════════════
   TOP ROW — prices + stats
══════════════════════════════════════════ */
#top {
  display: grid; grid-template-columns: 1fr 1fr 1fr; flex-shrink: 0;
  border-bottom: 1px solid var(--border2);
}

.price-cell {
  padding: 16px 20px; border-right: 1px solid var(--border2);
  position: relative; overflow: hidden;
}
.price-cell:last-child { border-right: none; }

/* Ghost symbol watermark */
.price-cell::after {
  content: attr(data-sym);
  position: absolute; right: -8px; top: 50%; transform: translateY(-50%);
  font-size: 58px; font-weight: 900; letter-spacing: -0.04em;
  color: var(--dim2); pointer-events: none; line-height: 1; opacity: 0.6;
  user-select: none;
}

.pc-label {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 9px; letter-spacing: 0.2em; color: var(--dim);
  text-transform: uppercase; margin-bottom: 9px;
}
.pc-feed { display: flex; align-items: center; gap: 5px; }
.feed-dot { width: 5px; height: 5px; border-radius: 50%; }
.feed-dot.fresh { background: var(--white2); box-shadow: 0 0 4px rgba(255,255,255,0.4); }
.feed-dot.stale { background: var(--red); animation: throb 1s infinite; }
.feed-txt { font-size: 9px; letter-spacing: 0.12em; }
.feed-txt.fresh { color: var(--white2); }
.feed-txt.stale { color: var(--red); }

.pc-price {
  font-size: 34px; font-weight: 700; letter-spacing: -0.02em; line-height: 1;
  margin-bottom: 11px; transition: color 0.18s;
}
.pc-changes { display: flex; gap: 18px; }
.pc-chg { display: flex; flex-direction: column; gap: 3px; }
.pc-chg-lbl { font-size: 8px; letter-spacing: 0.14em; color: var(--dim); text-transform: uppercase; }
.pc-chg-val { font-size: 13px; font-weight: 600; letter-spacing: 0.02em; }
.up   { color: var(--white); }
.dn   { color: var(--red); }
.flat { color: var(--dim); }
.fu { color: var(--white)  !important; text-shadow: 0 0 18px rgba(255,255,255,0.35); }
.fd { color: var(--red)    !important; text-shadow: 0 0 18px rgba(255,0,40,0.35); }

/* Stats cell */
.stats-cell { padding: 16px 20px; }
.sc-title { font-size: 9px; letter-spacing: 0.2em; color: var(--dim); text-transform: uppercase; margin-bottom: 12px; }
.stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 16px; }
.stat { display: flex; flex-direction: column; gap: 3px; }
.stat-lbl { font-size: 8px; letter-spacing: 0.16em; color: var(--dim); text-transform: uppercase; }
.stat-val { font-size: 24px; font-weight: 700; line-height: 1; color: var(--white); }
.stat-val.red { color: var(--red); }

/* ══════════════════════════════════════════
   MARKETS
══════════════════════════════════════════ */
#markets { flex-shrink: 0; border-bottom: 1px solid var(--border2); }
.sec-hdr {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 20px; background: var(--bg2);
  border-bottom: 1px solid var(--border);
}
.sec-title { font-size: 8px; font-weight: 700; letter-spacing: 0.22em; text-transform: uppercase; color: var(--dim); }
.sec-meta  { font-size: 9px; color: var(--dim); letter-spacing: 0.08em; }

#mkt-list { max-height: 172px; overflow-y: auto; padding: 0 20px; }
#mkt-list::-webkit-scrollbar { width: 2px; }
#mkt-list::-webkit-scrollbar-thumb { background: var(--dim2); }

.mkt-row {
  display: grid;
  grid-template-columns: 36px 1fr 108px 78px 60px 56px;
  gap: 10px; align-items: center;
  padding: 7px 0; border-bottom: 1px solid var(--border);
  font-size: 11px;
}
.mkt-row:last-child { border-bottom: none; }
.mkt-row:hover { background: var(--muted); margin: 0 -20px; padding: 7px 20px; }

.m-sym {
  font-size: 8px; font-weight: 700; letter-spacing: 0.1em;
  padding: 2px 5px; border-radius: 2px; text-align: center;
  border: 1px solid var(--red-dim); color: var(--red); background: var(--red-glow2);
}
.m-q { color: var(--white2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.m-tte { display: flex; flex-direction: column; gap: 4px; align-items: flex-end; }
.m-tte-num { font-size: 11px; font-weight: 600; }
.m-bar { width: 100px; height: 2px; background: var(--border2); border-radius: 1px; overflow: hidden; }
.m-bar-fill { height: 100%; border-radius: 1px; transition: width 1s linear; }
.m-impl { text-align: right; font-weight: 600; }
.m-spread { text-align: right; color: var(--dim); }
.m-depth { text-align: right; font-size: 9px; letter-spacing: 0.06em; }
.d-ok  { color: var(--dim); }
.d-low { color: var(--red); }

.no-data {
  padding: 22px 0; text-align: center;
  font-size: 10px; letter-spacing: 0.16em; color: var(--dim2);
  text-transform: uppercase;
}

/* ══════════════════════════════════════════
   BOTTOM — signals + log
══════════════════════════════════════════ */
#bottom { display: grid; grid-template-columns: 1fr 1fr; flex: 1; min-height: 0; }

#sig-section { border-right: 1px solid var(--border2); display: flex; flex-direction: column; min-height: 0; }
#sig-list { overflow-y: auto; flex: 1; }
#sig-list::-webkit-scrollbar { width: 2px; }
#sig-list::-webkit-scrollbar-thumb { background: var(--dim2); }

.sig-row {
  display: grid;
  grid-template-columns: 54px 72px 56px 52px 34px 1fr;
  gap: 8px; align-items: center;
  padding: 7px 20px;
  border-bottom: 1px solid var(--border);
  font-size: 11px;
  animation: sli 0.25s ease-out both;
}
@keyframes sli { from { opacity:0; transform: translateX(-6px); } to { opacity:1; transform: none; } }
.sig-row:last-child { border-bottom: none; }
.sig-row.traded { background: var(--red-glow2); border-left: 2px solid var(--red); padding-left: 18px; }

.s-ts  { color: var(--dim); font-size: 9px; }
.s-badge {
  font-size: 8px; font-weight: 700; letter-spacing: 0.08em;
  padding: 2px 0; border-radius: 2px; text-align: center; border: 1px solid;
}
.s-yes  { color: var(--red);    border-color: var(--red2);  background: var(--red-glow); }
.s-no   { color: var(--orange); border-color: #663300;       background: rgba(255,100,0,.05); }
.s-skip { color: var(--dim);    border-color: var(--border2); background: transparent; }
.s-edge { font-weight: 600; text-align: right; font-size: 11px; }
.s-edge.p { color: var(--red); }
.s-edge.n { color: var(--dim); }
.s-impl { color: var(--dim); text-align: right; }
.s-sym { font-size: 8px; font-weight: 700; color: var(--red); border: 1px solid var(--red-dim); padding: 1px 3px; border-radius: 2px; background: var(--red-glow2); text-align: center; }
.s-q   { color: var(--dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; }

#log-section { display: flex; flex-direction: column; min-height: 0; }
#log-body {
  flex: 1; overflow-y: auto; padding: 6px 12px;
  background: #040404; min-height: 0;
  font-size: 10px; line-height: 1.55;
}
#log-body::-webkit-scrollbar { width: 2px; }
#log-body::-webkit-scrollbar-thumb { background: var(--dim2); }
.ll { white-space: pre; font-family: var(--font); }
.ll.I { color: #2e2e2e; }
.ll.W { color: #6a3000; }
.ll.E { color: var(--red); }
.ll.T { color: var(--white2); font-weight: 600; }
</style>
</head>
<body>
<div id="app">

<!-- Header -->
<div id="hdr">
  <div id="logo"><div id="logo-dot"></div>TRADINGFANS</div>
  <div id="hdr-mid">
    <div class="kv">MAX <b id="h-max">—</b></div>
    <div class="kv">SYM <b id="h-sym">—</b></div>
    <div class="kv">POLL <b>5s</b></div>
    <div class="kv">NET <b>Polygon</b></div>
  </div>
  <div id="hdr-right">
    <span id="mode-badge" class="badge badge-dry">DRY RUN</span>
    <span id="uptime">00:00:00</span>
    <div id="conn"><div id="cdot" class="cdot-err"></div><span id="ctxt">connecting</span></div>
  </div>
</div>

<!-- Prices + Stats -->
<div id="top">
  <div class="price-cell" data-sym="BTC">
    <div class="pc-label">
      BTC / USD
      <div class="pc-feed">
        <div class="feed-dot stale" id="btc-dot"></div>
        <span class="feed-txt stale" id="btc-feed">—</span>
      </div>
    </div>
    <div class="pc-price" id="btc-price">—</div>
    <div class="pc-changes">
      <div class="pc-chg"><div class="pc-chg-lbl">1 MIN</div><div class="pc-chg-val flat" id="btc-1m">—</div></div>
      <div class="pc-chg"><div class="pc-chg-lbl">5 MIN</div><div class="pc-chg-val flat" id="btc-5m">—</div></div>
    </div>
  </div>

  <div class="price-cell" data-sym="ETH">
    <div class="pc-label">
      ETH / USD
      <div class="pc-feed">
        <div class="feed-dot stale" id="eth-dot"></div>
        <span class="feed-txt stale" id="eth-feed">—</span>
      </div>
    </div>
    <div class="pc-price" id="eth-price">—</div>
    <div class="pc-changes">
      <div class="pc-chg"><div class="pc-chg-lbl">1 MIN</div><div class="pc-chg-val flat" id="eth-1m">—</div></div>
      <div class="pc-chg"><div class="pc-chg-lbl">5 MIN</div><div class="pc-chg-val flat" id="eth-5m">—</div></div>
    </div>
  </div>

  <div class="price-cell stats-cell" data-sym="">
    <div class="sc-title">Session</div>
    <div class="stats-grid">
      <div class="stat"><div class="stat-lbl">Scans</div><div class="stat-val" id="st-scans">0</div></div>
      <div class="stat"><div class="stat-lbl">Markets</div><div class="stat-val" id="st-mkts">0</div></div>
      <div class="stat"><div class="stat-lbl">Signals</div><div class="stat-val" id="st-sigs">0</div></div>
      <div class="stat"><div class="stat-lbl">Trades</div><div class="stat-val red" id="st-trades">0</div></div>
    </div>
  </div>
</div>

<!-- Markets -->
<div id="markets">
  <div class="sec-hdr">
    <span class="sec-title">Active Scan Window</span>
    <span class="sec-meta" id="mkt-count">0 &lt; TTE &lt; 600s</span>
  </div>
  <div id="mkt-list"><div class="no-data">Scanning — no 5m crypto markets in window</div></div>
</div>

<!-- Signals + Log -->
<div id="bottom">
  <div id="sig-section">
    <div class="sec-hdr">
      <span class="sec-title">Signal Feed</span>
      <span class="sec-meta">newest first</span>
    </div>
    <div id="sig-list"><div class="no-data">No signals yet</div></div>
  </div>

  <div id="log-section">
    <div class="sec-hdr">
      <span class="sec-title">Engine Log</span>
      <span class="sec-meta" id="sse-lbl" style="color:var(--dim)">SSE ○</span>
    </div>
    <div id="log-body"></div>
  </div>
</div>

</div><!-- #app -->

<script>
const $ = id => document.getElementById(id);
let prevBtc = null, prevEth = null, lastLog = 0, fails = 0;

const fmt  = (n, d=2) => Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const pcls = v => v > 0.001 ? 'up' : v < -0.001 ? 'dn' : 'flat';
const pstr = v => (v >= 0 ? '+' : '') + fmt(v, 3) + '%';
const p2   = n => String(Math.floor(Math.max(0,n))).padStart(2,'0');

function setConn(ok) {
  $('cdot').className = ok ? 'cdot-ok' : 'cdot-err';
  $('ctxt').textContent = ok ? 'live' : 'reconnecting';
  $('ctxt').style.color = ok ? 'var(--white2)' : 'var(--red)';
}

function flash(id, nv, pv) {
  if (pv === null || nv === pv) return;
  const el = $(id), c = nv > pv ? 'fu' : 'fd';
  el.classList.add(c);
  setTimeout(() => el.classList.remove(c), 450);
}

function renderQuote(sym, q) {
  if (!q) return;
  const s = sym.toLowerCase(), prev = sym === 'BTC' ? prevBtc : prevEth;
  const p = q.price;
  const priceEl = $(`${s}-price`);
  priceEl.textContent = '$' + fmt(p, p > 999 ? 0 : 2);
  flash(`${s}-price`, p, prev);
  if (sym === 'BTC') prevBtc = p; else prevEth = p;

  const [c1, c5] = [q.change_1m_pct, q.change_5m_pct];
  const el1 = $(`${s}-1m`), el5 = $(`${s}-5m`);
  el1.className = `pc-chg-val ${pcls(c1)}`; el1.textContent = pstr(c1);
  el5.className = `pc-chg-val ${pcls(c5)}`; el5.textContent = pstr(c5);

  $(`${s}-dot`).className   = `feed-dot ${q.fresh ? 'fresh' : 'stale'}`;
  $(`${s}-feed`).className  = `feed-txt ${q.fresh ? 'fresh' : 'stale'}`;
  $(`${s}-feed`).textContent = q.fresh ? 'LIVE' : 'STALE';
}

function renderMarkets(ms) {
  const el = $('mkt-list');
  if (!ms || !ms.length) {
    $('mkt-count').textContent = '0 < TTE < 600s';
    el.innerHTML = '<div class="no-data">Scanning — no 5m crypto markets in window</div>';
    return;
  }
  $('mkt-count').textContent = `${ms.length} market${ms.length!==1?'s':''} · TTE < 600s`;
  el.innerHTML = ms.map(m => {
    const tte = Math.max(0, m.tte);
    const pct = Math.min(100, (tte / 600) * 100);
    const tc  = tte < 90 ? 'var(--red)' : tte < 200 ? 'var(--orange)' : 'var(--white2)';
    const fc  = tte < 90 ? 'var(--red)' : tte < 200 ? 'var(--orange)' : 'var(--dim)';
    const impl = (m.implied_yes * 100).toFixed(1);
    const ic   = Math.abs(m.implied_yes - 0.5) > 0.06 ? (m.implied_yes > 0.5 ? 'var(--red)' : 'var(--white2)') : 'var(--white2)';
    return `<div class="mkt-row">
      <div class="m-sym">${m.symbol}</div>
      <div class="m-q" title="${m.question}">${m.question}</div>
      <div class="m-tte">
        <span class="m-tte-num" style="color:${tc}">${p2(tte/60)}:${p2(tte%60)}</span>
        <div class="m-bar"><div class="m-bar-fill" style="width:${pct}%;background:${fc}"></div></div>
      </div>
      <div class="m-impl" style="color:${ic}">${impl}% YES</div>
      <div class="m-spread">${(m.spread*100).toFixed(2)}%</div>
      <div class="m-depth"><span class="${m.depth_ok?'d-ok':'d-low'}">${m.depth_ok?'✓ OK':'✗ LOW'}</span></div>
    </div>`;
  }).join('');
}

function renderSignals(ss) {
  const el = $('sig-list');
  if (!ss || !ss.length) { el.innerHTML = '<div class="no-data">No signals yet</div>'; return; }
  el.innerHTML = ss.map(s => {
    const bc = s.signal==='BUY_YES' ? 's-yes' : s.signal==='BUY_NO' ? 's-no' : 's-skip';
    const lbl = s.signal==='BUY_YES' ? 'BUY YES' : s.signal==='BUY_NO' ? 'BUY NO' : 'NO TRADE';
    const ec  = s.edge >= 0 ? 'p' : 'n';
    const estr = (s.edge>=0?'+':'') + (s.edge*100).toFixed(2)+'%';
    const traded = s.order_id ? ' traded' : '';
    return `<div class="sig-row${traded}">
      <span class="s-ts">${s.ts}</span>
      <span class="s-badge ${bc}">${lbl}</span>
      <span class="s-edge ${ec}">${estr}</span>
      <span class="s-impl">${(s.implied_yes*100).toFixed(1)}%</span>
      <span class="s-sym">${s.symbol}</span>
      <span class="s-q" title="${s.question}">${s.question}</span>
    </div>`;
  }).join('');
}

async function poll() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) throw 0;
    const d = await r.json();
    fails = 0; setConn(true);

    $('uptime').textContent = d.uptime;
    $('h-max').textContent  = '$' + d.max_size;
    $('h-sym').textContent  = d.symbol_filter;

    const b = $('mode-badge');
    b.className   = d.dry_run ? 'badge badge-dry' : 'badge badge-live';
    b.textContent = d.dry_run ? 'DRY RUN' : '● LIVE';

    renderQuote('BTC', d.btc);
    renderQuote('ETH', d.eth);

    $('st-scans').textContent  = d.scan_count;
    $('st-mkts').textContent   = d.active_markets ? d.active_markets.length : 0;
    $('st-sigs').textContent   = d.recent_signals ? d.recent_signals.length : 0;
    $('st-trades').textContent = d.trade_count;

    renderMarkets(d.active_markets);
    renderSignals(d.recent_signals);

    if (d.log_lines && d.log_lines.length > lastLog) {
      appendLogs(d.log_lines.slice(lastLog));
      lastLog = d.log_lines.length;
    }
  } catch { if (++fails > 2) setConn(false); }
}

function appendLogs(lines) {
  const c = $('log-body');
  const atBot = c.scrollHeight - c.scrollTop - c.clientHeight < 40;
  lines.forEach(line => {
    const d = document.createElement('div');
    const lvl = line.includes('ERROR') ? 'E' : line.includes('WARNING') ? 'W'
              : (line.includes('TRADE') || line.includes('\uD83D\uDD14')) ? 'T' : 'I';
    d.className = `ll ${lvl}`;
    d.textContent = line;
    c.appendChild(d);
  });
  while (c.children.length > 600) c.removeChild(c.firstChild);
  if (atBot) c.scrollTop = c.scrollHeight;
}

function startSSE() {
  const es = new EventSource('/api/stream');
  $('sse-lbl').textContent = 'SSE ●'; $('sse-lbl').style.color = 'var(--red)';
  es.onmessage = e => { appendLogs([JSON.parse(e.data)]); lastLog++; };
  es.onerror   = () => {
    $('sse-lbl').textContent = 'SSE ○'; $('sse-lbl').style.color = 'var(--dim)';
    es.close(); setTimeout(startSSE, 3000);
  };
}

poll(); setInterval(poll, 2000); startSSE();
</script>
</body>
</html>
"""


# ── Handlers ──────────────────────────────────────────────────

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=DASHBOARD_HTML, content_type="text/html")


async def handle_state(request: web.Request) -> web.Response:
    return web.json_response(STATE.to_dict())


async def handle_stream(request: web.Request) -> web.StreamResponse:
    """SSE endpoint — streams new log lines as they arrive."""
    response = web.StreamResponse()
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Access-Control-Allow-Origin"] = "*"
    await response.prepare(request)

    sent = len(STATE.log_lines)
    try:
        while True:
            current = list(STATE.log_lines)
            if len(current) > sent:
                for line in current[sent:]:
                    payload = f"data: {json.dumps(line)}\n\n"
                    await response.write(payload.encode())
                sent = len(current)
            await asyncio.sleep(0.25)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return response


# ── Server lifecycle ──────────────────────────────────────────

async def start_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/stream", handle_stream)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", UI_PORT)
    await site.start()
    log.info("Dashboard: http://localhost:%d", UI_PORT)
    return runner
