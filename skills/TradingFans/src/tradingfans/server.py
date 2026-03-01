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
<style>
  :root {
    --bg:      #080808;
    --panel:   #111111;
    --border:  #1e1e1e;
    --red:     #e8000d;
    --red-dim: #7a0007;
    --white:   #f0f0f0;
    --gray:    #555555;
    --dim:     #333333;
    --mono:    'Courier New', 'Consolas', monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--white);
    font-family: var(--mono);
    font-size: 13px;
    min-height: 100vh;
  }

  /* ── Header ── */
  #header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 20px;
    border-bottom: 1px solid var(--border);
    background: #0d0d0d;
  }
  #logo { font-size: 18px; font-weight: bold; letter-spacing: 0.12em; color: var(--white); }
  #logo span { color: var(--red); }
  .badge {
    display: inline-block; padding: 2px 8px;
    border-radius: 2px; font-size: 11px; font-weight: bold;
    letter-spacing: 0.1em; text-transform: uppercase;
  }
  .badge-dry  { background: var(--red-dim); color: #ff6066; border: 1px solid var(--red); }
  .badge-live { background: #001a00; color: #00ff44; border: 1px solid #00cc33; }
  #uptime { color: var(--gray); font-size: 12px; }

  /* ── Layout ── */
  #main { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1px; background: var(--border); }
  #bottom { display: grid; grid-template-columns: 1fr 1fr; gap: 1px; background: var(--border); }
  #full   { background: var(--border); }
  .panel {
    background: var(--panel);
    padding: 14px 16px;
  }
  .panel-title {
    font-size: 10px; font-weight: bold; letter-spacing: 0.18em;
    text-transform: uppercase; color: var(--gray);
    border-bottom: 1px solid var(--border);
    padding-bottom: 8px; margin-bottom: 12px;
  }

  /* ── Price cards ── */
  .price-value {
    font-size: 32px; font-weight: bold; letter-spacing: -0.01em;
    transition: color 0.3s;
  }
  .price-change { font-size: 12px; margin-top: 4px; }
  .price-1m, .price-5m { display: inline-block; margin-right: 12px; }
  .dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    margin-right: 5px; vertical-align: middle;
  }
  .dot-fresh { background: var(--white); box-shadow: 0 0 6px #ffffff88; }
  .dot-stale { background: var(--red); box-shadow: 0 0 6px var(--red); animation: pulse 1s infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

  .up   { color: var(--white); }
  .down { color: var(--red); }
  .flat { color: var(--gray); }

  .flash-up   { color: var(--white) !important; }
  .flash-down { color: var(--red) !important; }

  /* ── Stats ── */
  .stat-row { display: flex; justify-content: space-between; margin-bottom: 8px; }
  .stat-label { color: var(--gray); text-transform: uppercase; font-size: 11px; letter-spacing: 0.1em; }
  .stat-value { color: var(--white); font-size: 18px; font-weight: bold; }
  .stat-value.red { color: var(--red); }

  /* ── Markets table ── */
  .market-row {
    display: grid;
    grid-template-columns: 40px 1fr 70px 70px 60px 50px;
    gap: 8px;
    padding: 6px 0;
    border-bottom: 1px solid var(--border);
    align-items: center;
    font-size: 12px;
  }
  .market-row:last-child { border-bottom: none; }
  .market-sym {
    font-weight: bold; color: var(--red);
    font-size: 11px; letter-spacing: 0.08em;
  }
  .market-q { color: var(--white); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .market-tte { color: var(--gray); text-align: right; }
  .market-impl { text-align: right; }
  .market-spread { color: var(--gray); text-align: right; }
  .no-markets { color: var(--dim); font-style: italic; padding: 8px 0; text-align: center; }

  /* ── Signal feed ── */
  .signal-row {
    display: grid;
    grid-template-columns: 55px 75px 70px 70px 1fr;
    gap: 6px; align-items: center;
    padding: 5px 0;
    border-bottom: 1px solid var(--border);
    font-size: 12px;
  }
  .signal-row:last-child { border-bottom: none; }
  .sig-ts { color: var(--gray); font-size: 11px; }
  .sig-label {
    font-weight: bold; font-size: 11px; letter-spacing: 0.06em;
    padding: 2px 6px; border-radius: 2px; text-align: center;
  }
  .sig-buy-yes { background: #1a0000; color: var(--red); border: 1px solid var(--red-dim); }
  .sig-buy-no  { background: #0d0d0d; color: #ff8888; border: 1px solid #440000; }
  .sig-no-trade{ background: var(--dim); color: var(--gray); border: 1px solid #2a2a2a; }
  .sig-edge { text-align: right; }
  .sig-edge.pos { color: var(--red); }
  .sig-edge.neg { color: var(--gray); }
  .sig-q { color: var(--gray); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }

  /* ── Log panel ── */
  #log-container {
    height: 220px; overflow-y: auto; font-size: 11px;
    background: #090909; padding: 8px;
    scroll-behavior: smooth;
  }
  #log-container::-webkit-scrollbar { width: 4px; }
  #log-container::-webkit-scrollbar-thumb { background: var(--dim); }
  .log-line { padding: 1px 0; white-space: pre; color: #888; }
  .log-line.info  { color: #666; }
  .log-line.warn  { color: #aa4400; }
  .log-line.error { color: var(--red); }
  .log-line.trade { color: var(--white); font-weight: bold; }

  /* ── Connection indicator ── */
  #conn-status {
    font-size: 10px; color: var(--gray);
    display: flex; align-items: center; gap: 5px;
  }
  #conn-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--gray); }
  #conn-dot.ok  { background: var(--white); }
  #conn-dot.err { background: var(--red); animation: pulse 1s infinite; }
</style>
</head>
<body>

<!-- Header -->
<div id="header">
  <div id="logo">TRADING<span>FANS</span></div>
  <div style="display:flex;align-items:center;gap:14px">
    <span id="mode-badge" class="badge badge-dry">DRY RUN</span>
    <span id="uptime" style="color:var(--gray);font-size:12px">—</span>
    <div id="conn-status">
      <div id="conn-dot"></div>
      <span id="conn-text">connecting</span>
    </div>
  </div>
</div>

<!-- Price + Stats row -->
<div id="main">
  <div class="panel" id="btc-panel">
    <div class="panel-title">BTC / USD</div>
    <div class="price-value" id="btc-price">—</div>
    <div class="price-change">
      <span class="price-1m" id="btc-1m">1m —</span>
      <span class="price-5m" id="btc-5m">5m —</span>
    </div>
    <div style="margin-top:10px;font-size:11px">
      <span class="dot" id="btc-dot"></span>
      <span id="btc-status" style="color:var(--gray)">—</span>
    </div>
  </div>

  <div class="panel" id="eth-panel">
    <div class="panel-title">ETH / USD</div>
    <div class="price-value" id="eth-price">—</div>
    <div class="price-change">
      <span class="price-1m" id="eth-1m">1m —</span>
      <span class="price-5m" id="eth-5m">5m —</span>
    </div>
    <div style="margin-top:10px;font-size:11px">
      <span class="dot" id="eth-dot"></span>
      <span id="eth-status" style="color:var(--gray)">—</span>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">Session Stats</div>
    <div class="stat-row">
      <span class="stat-label">Scans</span>
      <span class="stat-value" id="stat-scans">0</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Signals</span>
      <span class="stat-value" id="stat-signals">0</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Trades</span>
      <span class="stat-value red" id="stat-trades">0</span>
    </div>
    <div class="stat-row">
      <span class="stat-label">Markets</span>
      <span class="stat-value" id="stat-markets">0</span>
    </div>
  </div>
</div>

<!-- Markets row (full width) -->
<div id="full" style="background:var(--border)">
  <div class="panel">
    <div class="panel-title">Active Scan Window
      <span style="float:right;color:var(--dim)">0 &lt; TTE &lt; 600s</span>
    </div>
    <div id="markets-list">
      <div class="no-markets">Scanning — no 5m crypto markets in window</div>
    </div>
  </div>
</div>

<!-- Signals + Log row -->
<div id="bottom">
  <div class="panel">
    <div class="panel-title">Signal Feed <span style="color:var(--dim);float:right">newest first</span></div>
    <div id="signal-feed" style="max-height:280px;overflow-y:auto">
      <div class="no-markets">No signals yet</div>
    </div>
  </div>

  <div class="panel" style="padding-bottom:0">
    <div class="panel-title">Engine Log <span id="sse-badge" style="float:right;color:var(--dim)">SSE</span></div>
    <div id="log-container"></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let prevBtcPrice = null, prevEthPrice = null;
let lastLogCount = 0;
let pollFails = 0;

function fmt(n, dec=2) {
  return Number(n).toLocaleString('en-US', {minimumFractionDigits:dec, maximumFractionDigits:dec});
}

function pctClass(v) {
  if (v > 0.001) return 'up';
  if (v < -0.001) return 'down';
  return 'flat';
}

function pctStr(v) {
  const sign = v > 0 ? '+' : '';
  return `${sign}${fmt(v, 3)}%`;
}

function flashPrice(elId, newVal, prevVal) {
  const el = $(elId);
  if (prevVal === null) return;
  const cls = newVal > prevVal ? 'flash-up' : newVal < prevVal ? 'flash-down' : null;
  if (!cls) return;
  el.classList.add(cls);
  setTimeout(() => el.classList.remove(cls), 600);
}

function renderQuote(sym, q) {
  if (!q) return;
  const lsym = sym.toLowerCase();
  const priceEl = $(`${lsym}-price`);
  const prev = sym === 'BTC' ? prevBtcPrice : prevEthPrice;

  const price = q.price;
  priceEl.textContent = '$' + fmt(price, price > 1000 ? 0 : 2);
  flashPrice(`${lsym}-price`, price, prev);
  if (sym === 'BTC') prevBtcPrice = price; else prevEthPrice = price;

  const c1 = q.change_1m_pct;
  const c5 = q.change_5m_pct;
  $(`${lsym}-1m`).className = `price-1m ${pctClass(c1)}`;
  $(`${lsym}-1m`).textContent = `1m ${pctStr(c1)}`;
  $(`${lsym}-5m`).className = `price-5m ${pctClass(c5)}`;
  $(`${lsym}-5m`).textContent = `5m ${pctStr(c5)}`;

  const dot = $(`${lsym}-dot`);
  const statusEl = $(`${lsym}-status`);
  dot.className = `dot ${q.fresh ? 'dot-fresh' : 'dot-stale'}`;
  statusEl.textContent = q.fresh ? 'LIVE' : 'STALE';
  statusEl.style.color = q.fresh ? '#888' : 'var(--red)';
}

function renderMarkets(markets) {
  const el = $('markets-list');
  if (!markets || markets.length === 0) {
    el.innerHTML = '<div class="no-markets">Scanning — no 5m crypto markets in window</div>';
    return;
  }
  el.innerHTML = markets.map(m => {
    const tte = m.tte;
    const mm = Math.floor(tte/60), ss = Math.floor(tte%60);
    const tteStr = `${mm}:${ss.toString().padStart(2,'0')}`;
    const impl = (m.implied_yes * 100).toFixed(1) + '%';
    const spread = (m.spread * 100).toFixed(2) + '%';
    return `<div class="market-row">
      <div class="market-sym">${m.symbol}</div>
      <div class="market-q" title="${m.question}">${m.question}</div>
      <div class="market-tte" style="color:${tte<120?'var(--red)':'var(--gray)'}">⏱ ${tteStr}</div>
      <div class="market-impl" style="color:var(--white)">${impl} YES</div>
      <div class="market-spread">${spread}</div>
      <div style="color:${m.depth_ok?'#444':'var(--red)'}; font-size:11px; text-align:right">${m.depth_ok?'OK':'LOW LIQ'}</div>
    </div>`;
  }).join('');
}

function renderSignals(signals) {
  const el = $('signal-feed');
  if (!signals || signals.length === 0) {
    el.innerHTML = '<div class="no-markets">No signals yet</div>';
    return;
  }
  el.innerHTML = [...signals].reverse().map(s => {
    const sigClass = s.signal === 'BUY_YES' ? 'sig-buy-yes'
                   : s.signal === 'BUY_NO'  ? 'sig-buy-no'
                   : 'sig-no-trade';
    const edgeCls = s.edge >= 0 ? 'pos' : 'neg';
    const edgeStr = (s.edge >= 0 ? '+' : '') + (s.edge * 100).toFixed(2) + '%';
    return `<div class="signal-row">
      <div class="sig-ts">${s.ts}</div>
      <div class="sig-label ${sigClass}">${s.signal.replace('_',' ')}</div>
      <div class="sig-edge ${edgeCls}">${edgeStr}</div>
      <div style="color:var(--gray);text-align:right;font-size:11px">${(s.implied_yes*100).toFixed(1)}%</div>
      <div class="sig-q" title="${s.question}">[${s.symbol}] ${s.question}</div>
    </div>`;
  }).join('');
}

function setConn(ok) {
  const dot = $('conn-dot');
  const txt = $('conn-text');
  dot.className = ok ? 'ok' : 'err';
  txt.textContent = ok ? 'live' : 'reconnecting';
  txt.style.color = ok ? '#888' : 'var(--red)';
}

async function poll() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    pollFails = 0;
    setConn(true);

    // Header
    $('uptime').textContent = d.uptime;
    const badge = $('mode-badge');
    if (d.dry_run) {
      badge.className = 'badge badge-dry';
      badge.textContent = 'DRY RUN';
    } else {
      badge.className = 'badge badge-live';
      badge.textContent = '● LIVE';
    }

    // Prices
    renderQuote('BTC', d.btc);
    renderQuote('ETH', d.eth);

    // Stats
    $('stat-scans').textContent = d.scan_count;
    $('stat-signals').textContent = d.recent_signals ? d.recent_signals.length : 0;
    $('stat-trades').textContent = d.trade_count;
    $('stat-markets').textContent = d.active_markets ? d.active_markets.length : 0;

    // Markets
    renderMarkets(d.active_markets);

    // Signals
    renderSignals(d.recent_signals);

    // Logs (fallback for non-SSE)
    if (d.log_lines && d.log_lines.length > lastLogCount) {
      const newLines = d.log_lines.slice(lastLogCount);
      appendLogs(newLines);
      lastLogCount = d.log_lines.length;
    }

  } catch(e) {
    pollFails++;
    if (pollFails > 2) setConn(false);
  }
}

function appendLogs(lines) {
  const container = $('log-container');
  const atBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 40;
  lines.forEach(line => {
    const div = document.createElement('div');
    div.className = 'log-line' +
      (line.includes('ERROR') ? ' error' :
       line.includes('WARNING') ? ' warn' :
       line.includes('TRADE') || line.includes('🔔') ? ' trade' : ' info');
    div.textContent = line;
    container.appendChild(div);
  });
  if (atBottom) container.scrollTop = container.scrollHeight;
  // Trim old lines
  while (container.children.length > 500) container.removeChild(container.firstChild);
}

// SSE for live logs
function startSSE() {
  const es = new EventSource('/api/stream');
  $('sse-badge').style.color = 'var(--red)';
  $('sse-badge').textContent = '● SSE';

  es.onmessage = e => {
    const line = JSON.parse(e.data);
    appendLogs([line]);
    lastLogCount++;
  };
  es.onerror = () => {
    $('sse-badge').textContent = 'SSE ✗';
    $('sse-badge').style.color = 'var(--dim)';
    setTimeout(startSSE, 3000);
    es.close();
  };
}

// Boot
poll();
setInterval(poll, 2000);
startSSE();
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
