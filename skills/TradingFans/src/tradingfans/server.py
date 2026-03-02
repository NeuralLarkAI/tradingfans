"""
server.py — Async aiohttp dashboard server.

Serves a real-time trading terminal UI at http://localhost:POLY_UI_PORT (default 7331).

Endpoints:
  GET /           — HTML dashboard (tabbed: Dry Run | Mainnet)
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
from .agents import MAX_AGENTS, mutate, to_public_dict

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
  --green:     #00e676;
  --green-dim: rgba(0,230,118,0.08);
  --font: 'JetBrains Mono', 'Courier New', monospace;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  background: var(--bg);
  color: var(--white);
  font-family: var(--font);
  font-size: 12px;
  height: 100vh;
  overflow: hidden;
}
body::before {
  content: '';
  position: fixed; inset: 0;
  background-image: radial-gradient(circle, #181818 1px, transparent 1px);
  background-size: 24px 24px;
  pointer-events: none; opacity: 0.55; z-index: 0;
}
#app {
  position: relative; z-index: 1;
  display: flex; flex-direction: column;
  height: 100vh; overflow: hidden;
}

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
#logo { display: flex; align-items: center; gap: 10px; font-size: 15px; font-weight: 700; letter-spacing: 0.22em; }
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
.badge-live { background: #001a00; color: var(--green); border: 1px solid #00803a; box-shadow: 0 0 8px rgba(0,230,118,0.15); }
#uptime { color: var(--dim); font-size: 11px; letter-spacing: 0.08em; font-weight: 300; }
#conn { display: flex; align-items: center; gap: 5px; font-size: 10px; color: var(--dim); }
#cdot { width: 5px; height: 5px; border-radius: 50%; }
.cdot-ok  { background: var(--white2) !important; box-shadow: 0 0 5px rgba(255,255,255,0.3); }
.cdot-err { background: var(--red) !important; animation: throb 1s infinite; }

/* ══════════════════════════════════════════
   TOP — prices + stats
══════════════════════════════════════════ */
#top { display: grid; grid-template-columns: 1fr 1fr 1fr; flex-shrink: 0; border-bottom: 1px solid var(--border2); }
.price-cell { padding: 14px 20px; border-right: 1px solid var(--border2); position: relative; overflow: hidden; }
.price-cell:last-child { border-right: none; }
.price-cell::after {
  content: attr(data-sym);
  position: absolute; right: -8px; top: 50%; transform: translateY(-50%);
  font-size: 58px; font-weight: 900; letter-spacing: -0.04em;
  color: var(--dim2); pointer-events: none; line-height: 1; opacity: 0.6; user-select: none;
}
.pc-label { display: flex; align-items: center; justify-content: space-between; font-size: 9px; letter-spacing: 0.2em; color: var(--dim); text-transform: uppercase; margin-bottom: 7px; }
.pc-feed { display: flex; align-items: center; gap: 5px; }
.feed-dot { width: 5px; height: 5px; border-radius: 50%; }
.feed-dot.fresh { background: var(--white2); box-shadow: 0 0 4px rgba(255,255,255,0.4); }
.feed-dot.stale { background: var(--red); animation: throb 1s infinite; }
.feed-txt.fresh { color: var(--white2); }
.feed-txt.stale { color: var(--red); }
.pc-price { font-size: 30px; font-weight: 700; letter-spacing: -0.02em; line-height: 1; margin-bottom: 9px; transition: color 0.18s; }
.pc-changes { display: flex; gap: 18px; }
.pc-chg { display: flex; flex-direction: column; gap: 3px; }
.pc-chg-lbl { font-size: 8px; letter-spacing: 0.14em; color: var(--dim); text-transform: uppercase; }
.pc-chg-val { font-size: 12px; font-weight: 600; }
.up { color: var(--white); } .dn { color: var(--red); } .flat { color: var(--dim); }
.fu { color: var(--white) !important; text-shadow: 0 0 18px rgba(255,255,255,0.35); }
.fd { color: var(--red)   !important; text-shadow: 0 0 18px rgba(255,0,40,0.35); }
.stats-cell { padding: 14px 20px; }
.sc-title { font-size: 9px; letter-spacing: 0.2em; color: var(--dim); text-transform: uppercase; margin-bottom: 10px; }
.stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; }
.stat { display: flex; flex-direction: column; gap: 2px; }
.stat-lbl { font-size: 8px; letter-spacing: 0.16em; color: var(--dim); text-transform: uppercase; }
.stat-val { font-size: 22px; font-weight: 700; line-height: 1; }
.stat-val.red { color: var(--red); }

/* ══════════════════════════════════════════
   MARKETS
══════════════════════════════════════════ */
#markets { flex-shrink: 0; border-bottom: 1px solid var(--border2); }
.sec-hdr {
  display: flex; align-items: center; justify-content: space-between;
  padding: 5px 20px; background: var(--bg2); border-bottom: 1px solid var(--border);
  flex-shrink: 0;
}
.sec-title { font-size: 8px; font-weight: 700; letter-spacing: 0.22em; text-transform: uppercase; color: var(--dim); }
.sec-meta  { font-size: 9px; color: var(--dim); letter-spacing: 0.08em; }
#mkt-list { max-height: 130px; overflow-y: auto; padding: 0 20px; }
#mkt-list::-webkit-scrollbar { width: 2px; }
#mkt-list::-webkit-scrollbar-thumb { background: var(--dim2); }
.mkt-row {
  display: grid; grid-template-columns: 36px 1fr 108px 78px 60px 56px;
  gap: 10px; align-items: center; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 11px;
}
.mkt-row:last-child { border-bottom: none; }
.m-sym { font-size: 8px; font-weight: 700; letter-spacing: 0.1em; padding: 2px 5px; border-radius: 2px; text-align: center; border: 1px solid var(--red-dim); color: var(--red); background: var(--red-glow2); }
.m-q { color: var(--white2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.m-tte { display: flex; flex-direction: column; gap: 4px; align-items: flex-end; }
.m-tte-num { font-size: 11px; font-weight: 600; }
.m-bar { width: 100px; height: 2px; background: var(--border2); border-radius: 1px; overflow: hidden; }
.m-bar-fill { height: 100%; border-radius: 1px; transition: width 1s linear; }
.m-impl { text-align: right; font-weight: 600; }
.m-spread { text-align: right; color: var(--dim); }
.m-depth { text-align: right; font-size: 9px; }
.d-ok  { color: var(--dim); } .d-low { color: var(--red); }
.no-data { padding: 18px 0; text-align: center; font-size: 10px; letter-spacing: 0.16em; color: var(--dim2); text-transform: uppercase; }

/* ══════════════════════════════════════════
   TAB NAVIGATION
══════════════════════════════════════════ */
#tab-nav {
  display: flex; align-items: stretch; flex-shrink: 0;
  background: var(--bg2); border-bottom: 1px solid var(--border2);
}
.tab-btn {
  display: flex; align-items: center; gap: 7px;
  padding: 0 22px; height: 36px;
  font-family: var(--font); font-size: 10px; font-weight: 700;
  letter-spacing: 0.18em; text-transform: uppercase;
  border: none; border-bottom: 2px solid transparent;
  background: transparent; color: var(--dim); cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.tab-btn:hover { color: var(--white2); }
.tab-btn.active { color: var(--white); border-bottom-color: var(--red); }
.tab-btn.active .tb-dot { background: var(--red); box-shadow: 0 0 5px var(--red); }
.tb-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--dim); }
.tab-btn.tab-live.active { color: var(--green); border-bottom-color: var(--green); }
.tab-btn.tab-live.active .tb-dot { background: var(--green); box-shadow: 0 0 5px var(--green); }
.tab-btn.tab-tuner.active { color: var(--orange); border-bottom-color: var(--orange); }
.tab-btn.tab-tuner.active .tb-dot { background: var(--orange); box-shadow: 0 0 5px var(--orange); }
#tab-spacer { flex: 1; }
#tab-meta { display: flex; align-items: center; padding: 0 16px; font-size: 9px; color: var(--dim); gap: 16px; }

/* ══════════════════════════════════════════
   BOTTOM — tab content + log
══════════════════════════════════════════ */
#bottom { display: grid; grid-template-columns: 1fr 1fr; flex: 1; min-height: 0; }

/* ── Tab content (left column) ── */
#tab-content { display: flex; flex-direction: column; min-height: 0; border-right: 1px solid var(--border2); }
.tab-panel { display: none; flex-direction: column; flex: 1; min-height: 0; }
.tab-panel.active { display: flex; }

/* ── Balance row (Dry Run tab) ── */
#dry-balance {
  display: grid; grid-template-columns: repeat(5, 1fr);
  flex-shrink: 0; border-bottom: 1px solid var(--border2);
}
.bal-card { padding: 11px 16px; border-right: 1px solid var(--border2); }
.bal-card:last-child { border-right: none; }
.bal-lbl { font-size: 8px; letter-spacing: 0.2em; color: var(--dim); text-transform: uppercase; margin-bottom: 5px; }
.bal-val { font-size: 19px; font-weight: 700; line-height: 1; color: var(--white); }
.bal-val.pos  { color: var(--green); }
.bal-val.neg  { color: var(--red); }
.bal-val.muted { color: var(--white2); }
.bal-sub { font-size: 9px; color: var(--dim); margin-top: 3px; }

/* ── Trade table ── */
.trade-hdr {
  display: grid; grid-template-columns: 50px 52px 34px 68px 58px 52px 56px 1fr;
  gap: 8px; align-items: center;
  padding: 5px 16px; flex-shrink: 0;
  background: var(--bg2); border-bottom: 1px solid var(--border);
  font-size: 8px; letter-spacing: 0.16em; color: var(--dim); text-transform: uppercase;
}
.tuner-box {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border2);
}
.tuner-title { font-size: 9px; letter-spacing: 0.2em; color: var(--dim); text-transform: uppercase; margin-bottom: 6px; }
.tuner-status { font-size: 14px; font-weight: 700; }
.tuner-status.on  { color: var(--orange); }
.tuner-status.off { color: var(--dim); }
.tuner-meta { margin-top: 4px; font-size: 9px; color: var(--dim); }
.tuner-grid-hdr, .tuner-grid-row {
  display: grid;
  grid-template-columns: 90px 1fr 80px 80px 1fr;
  gap: 8px; align-items: center;
  padding: 6px 16px;
}
.tuner-grid-hdr {
  background: var(--bg2);
  border-bottom: 1px solid var(--border);
  font-size: 8px; letter-spacing: 0.16em; color: var(--dim); text-transform: uppercase;
}
.tuner-grid-row { border-bottom: 1px solid var(--border); }
.t-key { color: var(--white2); }
.t-auto { color: var(--orange); font-weight: 700; font-size: 9px; letter-spacing: 0.12em; }
.t-dim { color: var(--dim); }
.t-num { text-align: right; font-weight: 700; }
.t-reason { color: var(--white2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.trade-list { overflow-y: auto; flex: 1; min-height: 0; }
.trade-list::-webkit-scrollbar { width: 2px; }
.trade-list::-webkit-scrollbar-thumb { background: var(--dim2); }

/* Agents */
.agent-actions { display: flex; gap: 8px; padding: 10px 16px; border-bottom: 1px solid var(--border2); background: var(--bg2); flex-shrink: 0; align-items: center; }
.btn { border: 1px solid var(--border2); background: transparent; color: var(--white2); padding: 6px 10px; font-size: 9px; letter-spacing: 0.18em; text-transform: uppercase; cursor: pointer; }
.btn:hover { color: var(--white); border-color: var(--red-dim); }
.btn.green:hover { border-color: var(--green); }
.btn.red:hover { border-color: var(--red); }
.btn.orange:hover { border-color: var(--orange); }
.agents-hdr { display: grid; grid-template-columns: 80px 1fr 54px 70px 74px 64px 60px 60px 58px 58px; gap: 8px; padding: 6px 16px; background: var(--bg2); border-bottom: 1px solid var(--border); font-size: 8px; letter-spacing: 0.16em; color: var(--dim); text-transform: uppercase; flex-shrink: 0; }
.agent-row { display: grid; grid-template-columns: 80px 1fr 54px 70px 74px 64px 60px 60px 58px 58px; gap: 8px; padding: 9px 16px; border-bottom: 1px solid var(--border); align-items: center; }
.agent-row:last-child { border-bottom: none; }
.a-id { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 10px; color: var(--white2); }
.a-name { color: var(--white2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.a-brain { font-size: 9px; color: var(--dim); letter-spacing: 0.14em; text-transform: uppercase; }
.a-stat { text-align: right; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 10px; color: var(--white2); }
.a-pnl.pos { color: var(--green); }
.a-pnl.neg { color: var(--red); }
.a-chip { font-size: 8px; letter-spacing: 0.18em; padding: 2px 6px; border: 1px solid var(--border2); border-radius: 3px; color: var(--dim); text-transform: uppercase; margin-left: 6px; }
.a-chip.primary { border-color: var(--green); color: var(--green); }

.chart-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 10px;
  padding: 10px 16px;
  overflow-y: auto;
  flex: 1;
  min-height: 0;
}
.chart-card {
  border: 1px solid var(--border);
  background: var(--surface);
  padding: 10px 10px 8px 10px;
  border-radius: 4px;
  overflow: hidden;
}
.chart-hdr { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 6px; }
.chart-sym { font-size: 11px; font-weight: 700; letter-spacing: 0.12em; color: var(--white2); }
.chart-meta { font-size: 9px; color: var(--dim); }
.chart-svg { width: 100%; height: 88px; display: block; }
.chart-line { fill: none; stroke: var(--white2); stroke-width: 1.6; opacity: 0.9; }
.chart-fill { fill: rgba(255,255,255,0.06); stroke: none; }
.chart-axis { stroke: var(--border2); stroke-width: 1; opacity: 0.7; }
.trade-row {
  display: grid; grid-template-columns: 50px 52px 34px 68px 58px 52px 56px 1fr;
  gap: 8px; align-items: center;
  padding: 6px 16px; border-bottom: 1px solid var(--border); font-size: 11px;
  animation: sli 0.25s ease-out both;
}
@keyframes sli { from{opacity:0;transform:translateX(-6px)} to{opacity:1;transform:none} }
.trade-row:last-child { border-bottom: none; }
.trade-row:hover { background: var(--muted); }
.t-ts { color: var(--dim); font-size: 9px; }
.t-sym { font-size: 8px; font-weight: 700; letter-spacing: 0.1em; padding: 2px 4px; border-radius: 2px; text-align: center; border: 1px solid var(--red-dim); color: var(--red); background: var(--red-glow2); }
.t-badge { font-size: 8px; font-weight: 700; letter-spacing: 0.06em; padding: 2px 0; border-radius: 2px; text-align: center; border: 1px solid; }
.t-yes  { color: var(--red);    border-color: var(--red2);  background: var(--red-glow); }
.t-no   { color: var(--orange); border-color: #663300;       background: rgba(255,100,0,.05); }
.t-size { text-align: right; font-weight: 600; }
.t-price { text-align: right; color: var(--white2); }
.t-edge { text-align: right; font-weight: 600; }
.t-edge.p { color: var(--red); } .t-edge.n { color: var(--orange); }
.t-pnl { text-align: right; font-weight: 700; }
.t-pnl.pos { color: var(--green); } .t-pnl.neg { color: var(--red); }
.t-oid { color: var(--dim); font-size: 9px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ── Wallet card (Mainnet tab) ── */
#wallet-card {
  flex-shrink: 0; padding: 14px 16px;
  border-bottom: 1px solid var(--border2);
}
.wallet-top { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
.wallet-lbl { font-size: 8px; letter-spacing: 0.2em; color: var(--dim); text-transform: uppercase; }
.wallet-addr-txt { font-size: 11px; color: var(--white2); word-break: break-all; margin-bottom: 12px; }
.wallet-bals { display: flex; gap: 28px; }
.wbal { display: flex; flex-direction: column; gap: 3px; }
.wbal-lbl { font-size: 8px; letter-spacing: 0.16em; color: var(--dim); text-transform: uppercase; }
.wbal-val { font-size: 20px; font-weight: 700; line-height: 1; }
.wbal-val.usdc-val { color: var(--green); }
.wbal-val.matic-val { color: var(--white2); }
.copy-btn {
  padding: 3px 10px; border: 1px solid var(--border2);
  background: transparent; color: var(--dim);
  font-family: var(--font); font-size: 9px; letter-spacing: 0.12em;
  cursor: pointer; border-radius: 2px;
}
.copy-btn:hover { color: var(--white2); border-color: var(--dim); }
.mode-notice {
  flex-shrink: 0; padding: 12px 16px;
  border-bottom: 1px solid var(--border2);
  font-size: 11px; line-height: 1.65; color: var(--dim);
}
.mode-notice.dry b { color: var(--red-hi); }
.mode-notice.live b { color: var(--green); }
.mode-notice code { background: var(--surface2); padding: 1px 5px; border-radius: 2px; color: var(--white2); }

/* ── Log (right column) ── */
#log-section { display: flex; flex-direction: column; min-height: 0; }
#log-body {
  flex: 1; overflow-y: auto; padding: 6px 12px;
  background: #040404; min-height: 0; font-size: 10px; line-height: 1.55;
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

<!-- HEADER -->
<div id="hdr">
  <div id="logo"><div id="logo-dot"></div>TRADINGFANS</div>
  <div id="hdr-mid">
    <div class="kv">MAX <b id="h-max">—</b></div>
    <div class="kv">SYM <b id="h-sym">—</b></div>
    <div class="kv">POLL <b id="h-poll">—</b></div>
    <div class="kv">MIN EDGE <b id="h-edge">—</b></div>
    <div class="kv">MIN SIZE <b id="h-minsz">—</b></div>
    <div class="kv">NET <b>Polygon</b></div>
  </div>
  <div id="hdr-right">
    <span id="mode-badge" class="badge badge-dry">DRY RUN</span>
    <span id="uptime">00:00:00</span>
    <div id="conn"><div id="cdot" class="cdot-err"></div><span id="ctxt">connecting</span></div>
  </div>
</div>

<!-- TOP: PRICES + STATS -->
<div id="top">
  <div class="price-cell" data-sym="BTC">
    <div class="pc-label">BTC / USD
      <div class="pc-feed"><div class="feed-dot stale" id="btc-dot"></div><span class="feed-txt stale" id="btc-feed">—</span></div>
    </div>
    <div class="pc-price" id="btc-price">—</div>
    <div class="pc-changes">
      <div class="pc-chg"><div class="pc-chg-lbl">1 MIN</div><div class="pc-chg-val flat" id="btc-1m">—</div></div>
      <div class="pc-chg"><div class="pc-chg-lbl">5 MIN</div><div class="pc-chg-val flat" id="btc-5m">—</div></div>
    </div>
  </div>
  <div class="price-cell" data-sym="ETH">
    <div class="pc-label">ETH / USD
      <div class="pc-feed"><div class="feed-dot stale" id="eth-dot"></div><span class="feed-txt stale" id="eth-feed">—</span></div>
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

<!-- MARKETS -->
<div id="markets">
  <div class="sec-hdr">
    <span class="sec-title">Active Scan Window</span>
    <span class="sec-meta" id="mkt-count">0 &lt; TTE</span>
  </div>
  <div id="mkt-list"><div class="no-data">Scanning — no 5m crypto markets in window</div></div>
</div>

<!-- TAB NAVIGATION -->
<div id="tab-nav">
  <button class="tab-btn active" id="tbtn-dry" onclick="switchTab('dry')">
    <div class="tb-dot"></div>DRY RUN · $10K
  </button>
  <button class="tab-btn" id="tbtn-charts" onclick="switchTab('charts')">
    <div class="tb-dot"></div>CHARTS
  </button>
  <button class="tab-btn" id="tbtn-history" onclick="switchTab('history')">
    <div class="tb-dot"></div>HISTORY
  </button>
  <button class="tab-btn" id="tbtn-agents" onclick="switchTab('agents')">
    <div class="tb-dot"></div>AGENTS
  </button>
  <button class="tab-btn tab-live" id="tbtn-live" onclick="switchTab('live')">
    <div class="tb-dot"></div>MAINNET
  </button>
  <button class="tab-btn tab-tuner" id="tbtn-tuner" onclick="switchTab('tuner')">
    <div class="tb-dot"></div>TUNER
  </button>
  <div id="tab-spacer"></div>
  <div id="tab-meta">
    <span id="tmeta-dry">0 trades · $0.00 deployed</span>
    <span id="tmeta-charts" style="display:none">0 symbols</span>
    <span id="tmeta-history" style="display:none">0 resolved</span>
    <span id="tmeta-agents" style="display:none">0 agents</span>
    <span id="tmeta-live" style="display:none">0 live trades</span>
    <span id="tmeta-tuner" style="display:none">Tuner OFF</span>
  </div>
</div>

<!-- BOTTOM: TAB CONTENT + LOG -->
<div id="bottom">

  <!-- LEFT: Tab content -->
  <div id="tab-content">

    <!-- ══ DRY RUN PANEL ══ -->
    <div id="panel-dry" class="tab-panel active">

      <!-- Balance summary row -->
      <div id="dry-balance">
        <div class="bal-card">
          <div class="bal-lbl">STARTING</div>
          <div class="bal-val muted">$10,000</div>
          <div class="bal-sub">Virtual capital</div>
        </div>
        <div class="bal-card">
          <div class="bal-lbl">DEPLOYED</div>
          <div class="bal-val" id="dr-dep">$0.00</div>
          <div class="bal-sub" id="dr-dep-sub">0 trades at risk</div>
        </div>
        <div class="bal-card">
          <div class="bal-lbl">AVAILABLE</div>
          <div class="bal-val muted" id="dr-avl">$10,000</div>
          <div class="bal-sub">Uncommitted</div>
        </div>
        <div class="bal-card">
          <div class="bal-lbl">REALIZED PnL</div>
          <div class="bal-val pos" id="dr-rpnl">$0.00</div>
          <div class="bal-sub" id="dr-rpnl-sub">from resolved markets</div>
        </div>
        <div class="bal-card">
          <div class="bal-lbl">EXPECTED PnL</div>
          <div class="bal-val pos" id="dr-epnl">$0.00</div>
          <div class="bal-sub" id="dr-epnl-sub">expected vs market</div>
        </div>
      </div>

      <!-- Open trades header -->
      <div class="trade-hdr">
        <span>TIME</span><span>TTE</span><span>SYM</span><span>SIDE</span>
        <span style="text-align:right">SIZE</span>
        <span style="text-align:right">PRICE</span>
        <span style="text-align:right">AGENT</span>
        <span>QUESTION</span>
      </div>

      <!-- Trade list -->
      <div id="open-trade-list" class="trade-list">
        <div class="no-data">No open trades — waiting for signals near expiry</div>
      </div>
    </div><!-- /panel-dry -->

    <!-- ══ CHARTS PANEL ══ -->
    <div id="panel-charts" class="tab-panel">
      <div class="sec-hdr">
        <span class="sec-title">Spot Charts</span>
        <span class="sec-meta" id="charts-meta">—</span>
      </div>
      <div id="chart-grid" class="chart-grid"></div>
    </div><!-- /panel-charts -->

    <!-- ══ HISTORY PANEL ══ -->
    <div id="panel-history" class="tab-panel">
      <div class="sec-hdr">
        <span class="sec-title">Resolved Trades</span>
        <span class="sec-meta" id="hist-meta">0 resolved</span>
      </div>

      <div class="trade-hdr">
        <span>TIME</span><span>SYM</span><span>SIDE</span>
        <span style="text-align:right">SIZE</span>
        <span style="text-align:right">PRICE</span>
        <span style="text-align:right">OUT</span>
        <span style="text-align:right">PnL</span>
        <span>QUESTION</span>
      </div>

      <div id="history-trade-list" class="trade-list">
        <div class="no-data">No resolved trades yet</div>
      </div>
    </div><!-- /panel-history -->

    <!-- ══ AGENTS PANEL ══ -->
    <div id="panel-agents" class="tab-panel">
      <div class="sec-hdr">
        <span class="sec-title">Agent Pool</span>
        <span class="sec-meta" id="agents-meta">—</span>
      </div>
      <div class="agent-actions">
        <button class="btn orange" onclick="agentSpawn()">SPAWN</button>
        <button class="btn" onclick="agentClearPrimary()">CLEAR PRIMARY</button>
        <button class="btn" onclick="agentToggleEvolution()">TOGGLE EVOLVE</button>
        <div class="t-dim" style="margin-left:auto" id="agents-note">Max 5 logical agents · shared memory</div>
      </div>
      <div class="agents-hdr">
        <span>ID</span><span>NAME</span><span>BRAIN</span><span class="t-num">TRADES</span><span class="t-num">RESOLVED</span><span class="t-num">PNL</span>
        <span class="t-num">MINEDGE</span><span class="t-num">MAX$</span><span class="t-num">MIN$</span><span class="t-num">SCALE</span>
      </div>
      <div id="agent-list" class="trade-list">
        <div class="no-data">No agents yet</div>
      </div>
    </div><!-- /panel-agents -->

    <!-- ══ MAINNET PANEL ══ -->
    <div id="panel-live" class="tab-panel">

      <!-- Wallet card -->
      <div id="wallet-card">
        <div class="wallet-top">
          <span class="wallet-lbl">Wallet Address (Polygon)</span>
          <button class="copy-btn" onclick="copyWallet()">COPY ADDRESS</button>
        </div>
        <div class="wallet-addr-txt" id="mn-addr">loading...</div>
        <div class="wallet-bals">
          <div class="wbal">
            <div class="wbal-lbl">USDC Balance</div>
            <div class="wbal-val usdc-val" id="mn-usdc">—</div>
          </div>
          <div class="wbal">
            <div class="wbal-lbl">MATIC (gas)</div>
            <div class="wbal-val matic-val" id="mn-matic">—</div>
          </div>
          <div class="wbal">
            <div class="wbal-lbl">Status</div>
            <div class="wbal-val" id="mn-status" style="font-size:13px;color:var(--dim)">—</div>
          </div>
        </div>
      </div>

      <!-- Mode notice -->
      <div class="mode-notice dry" id="mn-notice">
        <b>DRY RUN MODE ACTIVE</b> — Live orders are disabled. Wallet is read-only.<br>
        To go live: restart the agent without <code>--dry-run</code> and ensure your wallet has USDC loaded.<br>
        To fund: send USDC on Polygon to the address above, then restart.
      </div>

      <!-- Live trade table header -->
      <div class="trade-hdr">
        <span>TIME</span><span>TTE</span><span>SYM</span><span>SIGNAL</span>
        <span style="text-align:right">SIZE</span>
        <span style="text-align:right">PRICE</span>
        <span style="text-align:right">EDGE</span>
        <span>ORDER ID</span>
      </div>

      <!-- Live trade list -->
      <div id="live-trade-list" class="trade-list">
        <div class="no-data">No mainnet trades — switch off dry-run to trade live</div>
      </div>
    </div><!-- /panel-live -->

    <!-- ?????? TUNER PANEL ?????? -->
    <div id="panel-tuner" class="tab-panel">
      <div class="tuner-box">
        <div class="tuner-title">Autotuner (bounded self-optimization)</div>
        <div class="tuner-status off" id="tu-status">OFF</div>
        <div class="tuner-meta" id="tu-meta">Allowlisted knobs only · bounded changes · persisted to config</div>
        <div class="tuner-meta" id="tu-remote">Telegram: OFF</div>
      </div>

      <div class="tuner-grid-hdr">
        <span>AUTO</span><span>PARAM</span><span class="t-num">MIN</span><span class="t-num">MAX</span><span>CURRENT</span>
      </div>
      <div id="tuner-param-list" class="trade-list">
        <div class="no-data">Waiting for tuner config...</div>
      </div>

      <div class="tuner-grid-hdr">
        <span>TIME</span><span>CHANGE</span><span class="t-num">OLD</span><span class="t-num">NEW</span><span>REASON</span>
      </div>
      <div id="tuner-event-list" class="trade-list">
        <div class="no-data">No tuning changes yet</div>
      </div>

      <div class="tuner-grid-hdr">
        <span>TIME</span><span>REMOTE</span><span style="grid-column: span 3">DETAIL</span>
      </div>
      <div id="remote-event-list" class="trade-list">
        <div class="no-data">No remote activity yet</div>
      </div>
    </div><!-- /panel-tuner -->

  </div><!-- #tab-content -->

  <!-- RIGHT: Log (always visible) -->
  <div id="log-section">
    <div class="sec-hdr">
      <span class="sec-title">Engine Log</span>
      <span class="sec-meta" id="sse-lbl" style="color:var(--dim)">SSE ○</span>
    </div>
    <div id="log-body"></div>
  </div>

</div><!-- #bottom -->
</div><!-- #app -->

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}[c]));
let prevBtc = null, prevEth = null, lastLog = 0, fails = 0;
let walletAddr = '';
let currentTab = 'dry';

const fmt  = (n, d=2) => Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const fmtD = (n, d=2) => (n >= 0 ? '+' : '') + '$' + fmt(Math.abs(n), d);
const pcls = v => v > 0.001 ? 'up' : v < -0.001 ? 'dn' : 'flat';
const pstr = v => (v >= 0 ? '+' : '') + fmt(v, 3) + '%';
const p2   = n => String(Math.floor(Math.max(0, n))).padStart(2, '0');
const fmtTte = sec => {
  sec = Math.max(0, Number(sec || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  return (h > 0 ? (h + ':' + p2(m)) : String(m)) + ':' + p2(s);
};

// ── Tab switching ──────────────────────────────────────────────
function switchTab(tab) {
  currentTab = tab;
  ['dry', 'charts', 'history', 'agents', 'live', 'tuner'].forEach(t => {
    $('tbtn-' + t).classList.toggle('active', t === tab);
    $('panel-' + t).classList.toggle('active', t === tab);
    const meta = $('tmeta-' + t);
    if (meta) meta.style.display = t === tab ? '' : 'none';
  });
}

// ── Connection ─────────────────────────────────────────────────
function setConn(ok) {
  $('cdot').className = ok ? 'cdot-ok' : 'cdot-err';
  $('ctxt').textContent = ok ? 'live' : 'reconnecting';
  $('ctxt').style.color = ok ? 'var(--white2)' : 'var(--red)';
}

// ── Price flash ────────────────────────────────────────────────
function flash(id, nv, pv) {
  if (pv === null || nv === pv) return;
  const el = $(id), c = nv > pv ? 'fu' : 'fd';
  el.classList.add(c);
  setTimeout(() => el.classList.remove(c), 450);
}

// ── Render spot quotes ─────────────────────────────────────────
function renderQuote(sym, q) {
  if (!q) return;
  const s = sym.toLowerCase(), prev = sym === 'BTC' ? prevBtc : prevEth;
  const p = q.price;
  const priceEl = $(`${s}-price`);
  priceEl.textContent = '$' + fmt(p, p > 999 ? 0 : 2);
  flash(`${s}-price`, p, prev);
  if (sym === 'BTC') prevBtc = p; else prevEth = p;
  const [c1, c5] = [q.change_1m_pct, q.change_5m_pct];
  $(`${s}-1m`).className = `pc-chg-val ${pcls(c1)}`; $(`${s}-1m`).textContent = pstr(c1);
  $(`${s}-5m`).className = `pc-chg-val ${pcls(c5)}`; $(`${s}-5m`).textContent = pstr(c5);
  $(`${s}-dot`).className  = `feed-dot ${q.fresh ? 'fresh' : 'stale'}`;
  $(`${s}-feed`).className = `feed-txt ${q.fresh ? 'fresh' : 'stale'}`;
  $(`${s}-feed`).textContent = q.fresh ? 'LIVE' : 'STALE';
}

// ── Render markets ─────────────────────────────────────────────
function renderMarkets(ms, maxTteSec) {
  maxTteSec = Math.max(1, Number(maxTteSec || 600));
  const el = $('mkt-list');
  if (!ms || !ms.length) {
    $('mkt-count').textContent = `0 < TTE < ${Math.round(maxTteSec)}s`;
    el.innerHTML = '<div class="no-data">Scanning — no 5m crypto markets in window</div>';
    return;
  }
  $('mkt-count').textContent = `${ms.length} market${ms.length!==1?'s':''} · TTE < ${Math.round(maxTteSec)}s`;
  el.innerHTML = ms.map(m => {
    const tte = Math.max(0, m.tte);
    const pct = Math.min(100, (tte / maxTteSec) * 100);
    const tc  = tte < 90 ? 'var(--red)' : tte < 200 ? 'var(--orange)' : 'var(--white2)';
    const fc  = tte < 90 ? 'var(--red)' : tte < 200 ? 'var(--orange)' : 'var(--dim)';
    const impl = (m.implied_yes * 100).toFixed(1);
    return `<div class="mkt-row">
      <div class="m-sym">${m.symbol}</div>
      <div class="m-q" title="${m.question}">${m.question}</div>
      <div class="m-tte">
        <span class="m-tte-num" style="color:${tc}">${p2(tte/60)}:${p2(tte%60)}</span>
        <div class="m-bar"><div class="m-bar-fill" style="width:${pct}%;background:${fc}"></div></div>
      </div>
      <div class="m-impl" style="color:${Math.abs(m.implied_yes-0.5)>0.06?(m.implied_yes>0.5?'var(--red)':'var(--white2)'):'var(--white2)'}">${impl}% YES</div>
      <div class="m-spread">${(m.spread*100).toFixed(2)}%</div>
      <div class="m-depth"><span class="${m.depth_ok?'d-ok':'d-low'}">${m.depth_ok?'✓ OK':'✗ LOW'}</span></div>
    </div>`;
  }).join('');
}

// ── Render dry-run balance ─────────────────────────────────────
function renderDryBalance(bal) {
  if (!bal) return;
  const { deployed, available, exp_pnl, realized_pnl, trade_count } = bal;
  $('dr-dep').textContent = '$' + fmt(deployed);
  $('dr-dep').className   = 'bal-val' + (deployed > 0 ? '' : '');
  $('dr-dep-sub').textContent = `${trade_count} trade${trade_count !== 1 ? 's' : ''} at risk`;
  $('dr-avl').textContent = '$' + fmt(available);

  const rEl = $('dr-rpnl');
  rEl.textContent = (realized_pnl >= 0 ? '+' : '') + '$' + fmt(Math.abs(realized_pnl));
  rEl.className   = 'bal-val ' + (realized_pnl >= 0 ? 'pos' : 'neg');
  $('dr-rpnl-sub').textContent = 'from resolved markets';

  const eEl = $('dr-epnl');
  eEl.textContent = (exp_pnl >= 0 ? '+' : '') + '$' + fmt(Math.abs(exp_pnl));
  eEl.className   = 'bal-val ' + (exp_pnl >= 0 ? 'pos' : 'neg');
  $('dr-epnl-sub').textContent = (exp_pnl >= 0 ? '▲ ' : '▼ ') + 'expected vs market';
  // Tab meta
  $('tmeta-dry').textContent = `${trade_count} trade${trade_count!==1?'s':''} · $${fmt(deployed)} deployed`;
}

// ── Render dry-run trades ──────────────────────────────────────
function renderOpenTrades(trades) {
  const el = $('open-trade-list');
  if (!el) return;
  if (!trades || !trades.length) {
    el.innerHTML = '<div class="no-data">No open trades — waiting for signals near expiry</div>';
    return;
  }
  el.innerHTML = trades
    .slice()
    .sort((a, b) => Number(a.tte_sec || 0) - Number(b.tte_sec || 0))
    .map(t => {
      const bc  = t.side === 'BUY_YES' ? 't-yes' : 't-no';
      const lbl = t.side === 'BUY_YES' ? 'BUY YES' : 'BUY NO';
      const ts = t.entry_epoch ? new Date(Number(t.entry_epoch) * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'}) : '—';
      return `<div class="trade-row">
        <span class="t-ts">${ts}</span>
        <span class="t-ts">${fmtTte(t.tte_sec)}</span>
        <span class="t-sym">${esc(String(t.symbol || ''))}</span>
        <span class="t-badge ${bc}">${lbl}</span>
        <span class="t-size">$${fmt(t.size_usdc)}</span>
        <span class="t-price">${Number(t.price_paid || 0).toFixed(1)}%</span>
        <span class="t-oid" title="${esc(String(t.market_id || ''))}">${esc(String(t.agent_id || ''))}</span>
        <span class="t-oid">${esc(String(t.question || ''))}</span>
      </div>`;
    }).join('');
}

async function postJson(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body || {})});
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return await r.json();
}
function agentPromote(id) { return postJson('/api/agents', {action:'promote', agent_id:id}).catch(()=>{}); }
function agentDrop(id) { return postJson('/api/agents', {action:'drop', agent_id:id}).catch(()=>{}); }
function agentSpawn() { return postJson('/api/agents', {action:'spawn'}).catch(()=>{}); }
function agentClearPrimary() { return postJson('/api/agents', {action:'clear_primary'}).catch(()=>{}); }
function agentToggleEvolution() { return postJson('/api/agents', {action:'toggle_evolution'}).catch(()=>{}); }

function renderAgents(multi) {
  const el = $('agent-list');
  const meta = $('agents-meta');
  if (!el) return;
  if (!multi || !multi.agents) {
    el.innerHTML = '<div class="no-data">Multi-agent disabled</div>';
    if (meta) meta.textContent = 'disabled';
    $('tmeta-agents').textContent = '0 agents';
    return;
  }
  const agents = multi.agents || [];
  const perf = multi.perf || {};
  const primary = String(multi.primary_agent_id || '');
  const evo = !!multi.evolution_enabled;
  if (meta) meta.textContent = `${agents.length} agent${agents.length!==1?'s':''} · primary=${primary||'—'} · evolve=${evo?'ON':'OFF'}`;
  $('tmeta-agents').textContent = `${agents.length} agent${agents.length!==1?'s':''}`;
  if (!agents.length) {
    el.innerHTML = '<div class="no-data">No agents yet</div>';
    return;
  }
  el.innerHTML = agents.map(a => {
    const id = String(a.agent_id || '');
    const p = perf[id] || {};
    const pnl = Number(p.realized_pnl || 0);
    const pc = pnl >= 0 ? 'pos' : 'neg';
    const chip = id && id === primary ? '<span class="a-chip primary">PRIMARY</span>' : '';
    const btns = `
      <button class="btn green" onclick="agentPromote('${esc(id)}')">PROMOTE</button>
      <button class="btn red" onclick="agentDrop('${esc(id)}')">DROP</button>
    `;
    return `<div class="agent-row">
      <span class="a-id">${esc(id)}</span>
      <span class="a-name" title="${esc(String(a.name||''))}">${esc(String(a.name||''))}${chip}</span>
      <span class="a-brain">${esc(String(a.brain||''))}</span>
      <span class="a-stat">${Number(p.trades||0)}</span>
      <span class="a-stat">${Number(p.resolved||0)}</span>
      <span class="a-stat a-pnl ${pc}">${pnl>=0?'+':''}$${fmt(Math.abs(pnl),2)}</span>
      <span class="a-stat">${Number(a.min_edge||0).toFixed(3)}</span>
      <span class="a-stat">${Number(a.max_size_usdc||0).toFixed(0)}</span>
      <span class="a-stat">${Number(a.min_order_size_usdc||0).toFixed(0)}</span>
      <span class="a-stat">${Number(a.edge_full_scale||0).toFixed(3)}</span>
    </div>
    <div class="agent-actions" style="border-bottom:none; padding-top:0; padding-bottom:10px">
      <div class="t-dim" style="flex:1">w1=${Number(a.w_m1||0).toFixed(1)} w5=${Number(a.w_m5||0).toFixed(1)} vol=${Number(a.w_vol||0).toFixed(1)}</div>
      ${btns}
    </div>`;
  }).join('');
}

function renderHistory(trades) {
  const el = $('history-trade-list');
  const meta = $('hist-meta');
  if (!el) return;
  if (!trades || !trades.length) {
    el.innerHTML = '<div class="no-data">No resolved trades yet</div>';
    if (meta) meta.textContent = '0 resolved';
    $('tmeta-history').textContent = '0 resolved';
    return;
  }
  if (meta) meta.textContent = `${trades.length} resolved`;
  $('tmeta-history').textContent = `${trades.length} resolved`;
  el.innerHTML = trades.map(t => {
    const ts = t.ts_epoch ? new Date(Number(t.ts_epoch) * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'}) : '—';
    const pnl = Number(t.pnl_usdc || 0);
    const pc  = pnl >= 0 ? 'p' : 'n';
    const bc  = t.side === 'BUY_YES' ? 't-yes' : 't-no';
    return `<div class="trade-row">
      <span class="t-ts">${ts}</span>
      <span class="t-sym">${esc(String(t.symbol || ''))}</span>
      <span class="t-badge ${bc}">${t.side === 'BUY_YES' ? 'YES' : 'NO'}</span>
      <span class="t-size">$${fmt(t.size_usdc)}</span>
      <span class="t-price">${Number(t.price_paid || 0).toFixed(1)}%</span>
      <span class="t-edge">${esc(String(t.outcome || ''))}</span>
      <span class="t-edge ${pc}">${pnl >= 0 ? '+' : ''}$${fmt(Math.abs(pnl))}</span>
      <span class="t-oid">${esc(String(t.question || ''))}</span>
    </div>`;
  }).join('');
}

function renderCharts(state) {
  const grid = $('chart-grid');
  if (!grid) return;

  const series = state?.spot_series || {};
  const syms = new Set();

  (state.active_markets || []).forEach(m => { if (m && m.symbol) syms.add(String(m.symbol)); });
  (state.performance?.open_trades || []).forEach(t => { if (t && t.symbol) syms.add(String(t.symbol)); });
  ['BTC','ETH'].forEach(s => { if (series[s]) syms.add(s); });

  const list = Array.from(syms).filter(s => series[s] && series[s].length >= 2).sort();
  $('tmeta-charts').textContent = `${list.length} symbol${list.length!==1?'s':''}`;
  const cm = $('charts-meta');
  if (cm) cm.textContent = `${list.length} symbol${list.length!==1?'s':''} · ${Object.keys(series).length} total in feed`;

  if (!list.length) {
    grid.innerHTML = '<div class="no-data" style="padding:10px 16px">No chart data yet.</div>';
    return;
  }

  const card = (sym, pts) => {
    const xs = pts.map(p => Number(p[0] || 0));
    const ys = pts.map(p => Number(p[1] || 0));
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    let y0 = Math.min(...ys), y1 = Math.max(...ys);
    if (!isFinite(y0) || !isFinite(y1) || y0 === y1) { y0 -= 1; y1 += 1; }
    const pad = (y1 - y0) * 0.05;
    y0 -= pad; y1 += pad;

    const W = 300, H = 88, P = 6;
    const nx = t => (P + (Math.max(0, Math.min(1, (t - x0) / Math.max(1, (x1 - x0)))) * (W - 2*P)));
    const ny = p => (P + (1 - Math.max(0, Math.min(1, (p - y0) / Math.max(1e-9, (y1 - y0))))) * (H - 2*P));

    const pl = pts.map(p => `${nx(Number(p[0]||0)).toFixed(1)},${ny(Number(p[1]||0)).toFixed(1)}`).join(' ');
    const first = ys[0], last = ys[ys.length - 1];
    const chg = (first && isFinite(first)) ? ((last/first - 1) * 100) : 0;
    const meta = `$${fmt(last, last>999?0:2)} · ${chg>=0?'+':''}${chg.toFixed(2)}%`;

    const fillPts = `${P},${H-P} ${pl} ${W-P},${H-P}`;
    return `<div class="chart-card">
      <div class="chart-hdr">
        <div class="chart-sym">${esc(sym)}</div>
        <div class="chart-meta">${esc(meta)}</div>
      </div>
      <svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        <line class="chart-axis" x1="${P}" y1="${H-P}" x2="${W-P}" y2="${H-P}"></line>
        <polyline class="chart-fill" points="${fillPts}"></polyline>
        <polyline class="chart-line" points="${pl}"></polyline>
      </svg>
    </div>`;
  };

  grid.innerHTML = list.map(sym => card(sym, series[sym])).join('');
}

// ── Render wallet card ─────────────────────────────────────────
function renderWallet(wallet, dryRun) {
  if (!wallet) return;
  walletAddr = wallet.address || '';
  const addrEl = $('mn-addr');
  addrEl.textContent = walletAddr || 'Not configured';

  if (wallet.usdc !== null && wallet.usdc !== undefined) {
    $('mn-usdc').textContent = '$' + fmt(wallet.usdc);
  } else {
    $('mn-usdc').textContent = 'Fetching...';
  }
  if (wallet.matic !== null && wallet.matic !== undefined) {
    $('mn-matic').textContent = fmt(wallet.matic, 4);
  } else {
    $('mn-matic').textContent = '—';
  }

  const hasBalance = wallet.usdc !== null && wallet.usdc > 0;
  const statusEl   = $('mn-status');
  const noticeEl   = $('mn-notice');

  if (dryRun) {
    statusEl.textContent = 'DRY RUN';
    statusEl.style.color = 'var(--red-hi)';
    noticeEl.className   = 'mode-notice dry';
    noticeEl.innerHTML   = `<b>DRY RUN MODE ACTIVE</b> — Live orders are disabled. Wallet is read-only.<br>
      To go live: restart the agent without <code>--dry-run</code> and ensure your wallet has USDC loaded.<br>
      To fund your wallet: send USDC on Polygon to the address above, then restart.`;
  } else if (!hasBalance) {
    statusEl.textContent = 'NEEDS FUNDS';
    statusEl.style.color = 'var(--orange)';
    noticeEl.className   = 'mode-notice';
    noticeEl.innerHTML   = '<b style="color:var(--orange)">LOW BALANCE</b> — Send USDC on Polygon to the wallet address above to begin trading.';
  } else {
    statusEl.textContent = '● LIVE';
    statusEl.style.color = 'var(--green)';
    noticeEl.className   = 'mode-notice live';
    noticeEl.innerHTML   = '<b>LIVE TRADING ACTIVE</b> — Real orders are being placed on Polymarket.';
  }
}

// ── Render live trades ─────────────────────────────────────────
function renderLiveTrades(trades, dryRun) {
  const el = $('live-trade-list');
  if (!trades || !trades.length) {
    el.innerHTML = dryRun
      ? '<div class="no-data">No mainnet trades — switch off dry-run to trade live</div>'
      : '<div class="no-data">No live trades yet this session</div>';
    return;
  }
  el.innerHTML = trades.map(t => {
    const bc  = t.signal === 'BUY_YES' ? 't-yes' : 't-no';
    const lbl = t.signal === 'BUY_YES' ? 'BUY YES' : 'BUY NO';
    const ec  = t.edge >= 0 ? 'p' : 'n';
    return `<div class="trade-row">
      <span class="t-ts">${t.ts}</span>
      <span class="t-ts">${fmtTte(t.tte_sec)}</span>
      <span class="t-sym">${t.symbol}</span>
      <span class="t-badge ${bc}">${lbl}</span>
      <span class="t-size">$${fmt(t.size_usdc)}</span>
      <span class="t-price">${t.price.toFixed(1)}%</span>
      <span class="t-edge ${ec}">${t.edge >= 0 ? '+' : ''}${t.edge.toFixed(2)}%</span>
      <span class="t-oid">${t.order_id}</span>
    </div>`;
  }).join('');
  $('tmeta-live').textContent = `${trades.length} live trade${trades.length!==1?'s':''}`;
}

// ── Copy wallet address ────────────────────────────────────────
// Render tuner
function renderTuner(tuner, dryRun) {
  const statusEl = $('tu-status');
  const metaEl   = $('tu-meta');
  const remoteEl = $('tu-remote');
  const pEl      = $('tuner-param-list');
  const eEl      = $('tuner-event-list');

  if (!tuner) {
    statusEl.textContent = 'OFF';
    statusEl.className = 'tuner-status off';
    metaEl.textContent = 'No tuner data available.';
    remoteEl.textContent = 'Telegram: OFF';
    pEl.innerHTML = '<div class="no-data">No tuner data</div>';
    eEl.innerHTML = '<div class="no-data">No tuning changes yet</div>';
    return;
  }

  const on = !!tuner.enabled;
  statusEl.textContent = on ? ('ON' + (dryRun ? ' (DRY RUN)' : ' (LIVE)')) : 'OFF';
  statusEl.className = 'tuner-status ' + (on ? 'on' : 'off');

  const path = tuner.config_path || '';
  const last = tuner.last_run_epoch ? new Date(tuner.last_run_epoch*1000).toLocaleTimeString() : '—';
  metaEl.innerHTML = `Config: <span class="t-dim">${path || '—'}</span> · Last run: <span class="t-dim">${last}</span>`;

  const minEdge = tuner.params?.['decision.min_edge'];
  $('tmeta-tuner').textContent = on
    ? `Tuner ON · min_edge=${(minEdge ?? 0).toFixed(3)}`
    : 'Tuner OFF';

  const specs = tuner.specs || [];
  const params = tuner.params || {};
  if (!specs.length) {
    pEl.innerHTML = '<div class="no-data">No tunable params found</div>';
  } else {
    pEl.innerHTML = specs.map(sp => {
      const cur = params[sp.key];
      const auto = sp.auto ? '<span class="t-auto">AUTO</span>' : '';
      return `<div class="tuner-grid-row">
        <span>${auto}</span>
        <span class="t-key" title="${sp.description || ''}">${sp.key}</span>
        <span class="t-num t-dim">${Number(sp.min).toFixed(3)}</span>
        <span class="t-num t-dim">${Number(sp.max).toFixed(3)}</span>
        <span class="t-dim">${cur === undefined ? '—' : Number(cur).toFixed(3)}</span>
      </div>`;
    }).join('');
  }

  const evs = tuner.events || [];
  if (!evs.length) {
    eEl.innerHTML = '<div class="no-data">No tuning changes yet</div>';
  } else {
    eEl.innerHTML = evs.slice(0, 100).map(ev => {
      const t = ev.ts_epoch ? new Date(ev.ts_epoch*1000).toLocaleTimeString() : '—';
      return `<div class="tuner-grid-row">
        <span class="t-dim">${t}</span>
        <span class="t-key">${ev.key}</span>
        <span class="t-num">${Number(ev.old).toFixed(3)}</span>
        <span class="t-num">${Number(ev.new).toFixed(3)}</span>
        <span class="t-reason" title="${ev.reason || ''}">${ev.reason || ''}</span>
      </div>`;
    }).join('');
  }
}

function renderRemote(remote) {
  const el = $('tu-remote');
  if (!el) return;
  const tg = remote?.telegram;
  if (!tg) { el.textContent = 'Telegram: OFF'; return; }
  if (!tg.enabled) {
    el.innerHTML = `Telegram: <span class="t-dim">OFF</span> · set <code>TELEGRAM_BOT_TOKEN</code> then send <code>/pair</code> to the bot`;
    return;
  }
  const st = tg.status || 'ON';
  el.innerHTML = `Telegram: <span class="t-dim">${st}</span>`;
}

function renderRemoteEvents(events) {
  const el = $('remote-event-list');
  if (!el) return;
  if (!events || !events.length) {
    el.innerHTML = '<div class="no-data">No remote activity yet</div>';
    return;
  }
  el.innerHTML = events.slice(0, 80).map(ev => {
    const t = ev.ts_epoch ? new Date(ev.ts_epoch*1000).toLocaleTimeString() : '—';
    const k = esc(String(ev.kind || ''));
    const d = esc(String(ev.detail || ''));
    return `<div class="tuner-grid-row">
      <span class="t-dim">${t}</span>
      <span class="t-key">${k}</span>
      <span class="t-dim" style="grid-column: span 3">${d}</span>
    </div>`;
  }).join('');
}

function copyWallet() {
  if (!walletAddr) return;
  navigator.clipboard.writeText(walletAddr).then(() => {
    const btn = document.querySelector('.copy-btn');
    const orig = btn.textContent;
    btn.textContent = 'COPIED ✓';
    btn.style.color = 'var(--green)';
    setTimeout(() => { btn.textContent = orig; btn.style.color = ''; }, 1500);
  });
}

// ── Log panel ─────────────────────────────────────────────────
function appendLogs(lines) {
  const c = $('log-body');
  const atBot = c.scrollHeight - c.scrollTop - c.clientHeight < 40;
  lines.forEach(line => {
    const d = document.createElement('div');
    const lvl = line.includes('ERROR') ? 'E' : line.includes('WARNING') ? 'W'
              : (line.includes('TRADE') || line.includes('\uD83D\uDD14') || line.includes('DRY RUN')) ? 'T' : 'I';
    d.className = `ll ${lvl}`;
    d.textContent = line;
    c.appendChild(d);
  });
  while (c.children.length > 600) c.removeChild(c.firstChild);
  if (atBot) c.scrollTop = c.scrollHeight;
}

// ── Main poll ──────────────────────────────────────────────────
async function poll() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) throw 0;
    const d = await r.json();
    fails = 0; setConn(true);

    // Header
    $('uptime').textContent = d.uptime;
    $('h-max').textContent  = '$' + d.max_size;
    $('h-sym').textContent  = d.symbol_filter;
    $('h-poll').textContent = (d.poll_interval ?? 5).toFixed(1) + 's';
    $('h-edge').textContent = (d.min_edge ?? 0.02).toFixed(3);
    $('h-minsz').textContent = '$' + fmt((d.min_order_size ?? 0.5), 2);
    const b = $('mode-badge');
    b.className   = d.dry_run ? 'badge badge-dry' : 'badge badge-live';
    b.textContent = d.dry_run ? 'DRY RUN' : '● LIVE';

    // Tab live button label
    $('tbtn-live').querySelector('.tb-dot').style.background = d.dry_run ? '' : 'var(--green)';

    // Prices
    renderQuote('BTC', d.btc);
    renderQuote('ETH', d.eth);

    // Stats
    $('st-scans').textContent  = d.scan_count;
    $('st-mkts').textContent   = d.active_markets ? d.active_markets.length : 0;
    $('st-sigs').textContent   = d.recent_signals ? d.recent_signals.length : 0;
    $('st-trades').textContent = d.trade_count;

    // Strategy (show in tuner meta line)
    if (d.strategy && d.strategy.name) {
      $('tmeta-tuner').textContent = `Tuner ${d.tuner?.enabled ? 'ON' : 'OFF'} · strat=${d.strategy.name}`;
    }

    // Markets
    renderMarkets(d.active_markets, d.max_time_to_expiry);

    // Charts tab
    renderCharts(d);

    // Dry run tab (open positions only)
    renderDryBalance(d.dry_balance);
    renderOpenTrades(d.performance?.open_trades);

    // History tab
    renderHistory(d.performance?.resolved_trades);

    // Agents tab
    renderAgents(d.multi_agent);

    // Mainnet tab
    renderWallet(d.wallet, d.dry_run);
    renderLiveTrades(d.live_trades, d.dry_run);

    // Tuner tab
    renderTuner(d.tuner, d.dry_run);
    renderRemote(d.remote);
    renderRemoteEvents(d.remote_events);

    // Log
    if (d.log_lines && d.log_lines.length > lastLog) {
      appendLogs(d.log_lines.slice(lastLog));
      lastLog = d.log_lines.length;
    }
  } catch { if (++fails > 2) setConn(false); }
}

// ── SSE ────────────────────────────────────────────────────────
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


def _local_only(request: web.Request) -> bool:
    r = (request.remote or "").strip()
    return (r in ("", "127.0.0.1", "::1"))


async def handle_agents(request: web.Request) -> web.Response:
    """Local-only agent pool controls (used by the AGENTS tab)."""
    if not _local_only(request):
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        body = {}

    action = str(body.get("action") or "").strip().lower()
    agent_id = str(body.get("agent_id") or "").strip()

    pool = getattr(STATE, "_agent_pool", None)  # type: ignore[attr-defined]
    if not pool:
        return web.json_response({"ok": False, "error": "no_pool"}, status=400)

    perf = STATE.agent_perf or {}

    def pnl(aid: str) -> float:
        try:
            return float((perf.get(aid) or {}).get("realized_pnl", 0.0))
        except Exception:
            return 0.0

    if action == "promote":
        if not any(a.agent_id == agent_id for a in pool):
            return web.json_response({"ok": False, "error": "unknown_agent"}, status=400)
        STATE.primary_agent_id = agent_id
    elif action == "clear_primary":
        STATE.primary_agent_id = ""
    elif action == "toggle_evolution":
        STATE.agent_evolution_enabled = not bool(getattr(STATE, "agent_evolution_enabled", True))
    elif action == "drop":
        if len(pool) <= 1:
            return web.json_response({"ok": False, "error": "cannot_drop_last"}, status=400)
        pool2 = [a for a in pool if a.agent_id != agent_id]
        if len(pool2) == len(pool):
            return web.json_response({"ok": False, "error": "unknown_agent"}, status=400)
        if STATE.primary_agent_id == agent_id:
            STATE.primary_agent_id = ""
        pool = pool2
        STATE._agent_pool = pool  # type: ignore[attr-defined]
        STATE.agents = [to_public_dict(a) for a in pool]
    elif action == "spawn":
        base = max(pool, key=lambda a: pnl(a.agent_id))
        child = mutate(base)
        pool.append(child)
        pool = sorted(pool, key=lambda a: pnl(a.agent_id), reverse=True)[:MAX_AGENTS]
        STATE._agent_pool = pool  # type: ignore[attr-defined]
        STATE.agents = [to_public_dict(a) for a in pool]
        STATE.agent_perf.setdefault(child.agent_id, {"trades": 0, "resolved": 0, "realized_pnl": 0.0})
    else:
        return web.json_response({"ok": False, "error": "unknown_action"}, status=400)

    return web.json_response({"ok": True, "multi_agent": STATE.to_dict().get("multi_agent")})


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
    app.router.add_post("/api/agents", handle_agents)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", UI_PORT)
    await site.start()
    log.info("Dashboard: http://localhost:%d", UI_PORT)
    return runner
