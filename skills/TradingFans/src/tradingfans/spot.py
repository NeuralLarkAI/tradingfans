"""
spot.py — Spot price feed with WebSocket primary + REST fallback.

Primary:  Binance WebSocket (aggTrade streams for BTCUSDT / ETHUSDT)
Fallback: Coinbase public REST API polled every REST_POLL_INTERVAL seconds

Maintains a rolling 5-minute (300s) price window per symbol.

Public API:
    feed = SpotFeed()
    await feed.start()

    window = feed.window("BTC")      # list[(epoch_ts, price)] last 5 min
    fresh  = feed.is_fresh("BTC")    # True if last tick was ≤ STALE_THRESHOLD ago
    price  = feed.latest_price("BTC")
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Deque

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

# Binance endpoints — .us domain for US IPs (avoids HTTP 451 geo-block)
BINANCE_WS_GLOBAL = "wss://stream.binance.com:9443/stream"
BINANCE_WS_US     = "wss://stream.binance.us:9443/stream"

# Coinbase public REST API — no auth, works globally
COINBASE_REST = "https://api.coinbase.com/v2/prices/{symbol}-USD/spot"

STREAMS: dict[str, str] = {
    "BTC": "btcusdt@aggTrade",
    "ETH": "ethusdt@aggTrade",
}

# Coinbase symbols (REST fallback and multi-asset support).
# Note: Some assets may not have Binance-US WS streams available; REST still works.
COINBASE_SYMBOLS: dict[str, str] = {
    "BTC": "BTC",
    "ETH": "ETH",
    "SOL": "SOL",
    "XRP": "XRP",
    "ADA": "ADA",
    "DOGE": "DOGE",
    "AVAX": "AVAX",
    "LINK": "LINK",
    "MATIC": "MATIC",
    "DOT": "DOT",
    "LTC": "LTC",
    "BCH": "BCH",
    "ATOM": "ATOM",
    "UNI": "UNI",
    "AAVE": "AAVE",
    "ETC": "ETC",
    "XLM": "XLM",
    "ALGO": "ALGO",
    "NEAR": "NEAR",
    "FIL": "FIL",
}

WINDOW_SECONDS    = 1200   # rolling window (20m) to support expiry/PnL resolution lookups
STALE_THRESHOLD   = 15.0  # seconds before spot is considered stale
RECONNECT_DELAY   = 3.0   # seconds between WS reconnect attempts
REST_POLL_INTERVAL = 5.0  # seconds between REST polls


class SpotFeed:
    """Spot price feed: Binance WebSocket + Coinbase REST fallback."""

    def __init__(self) -> None:
        syms = sorted(set(STREAMS.keys()) | set(COINBASE_SYMBOLS.keys()))
        self._windows: dict[str, Deque[tuple[float, float]]] = {sym: deque() for sym in syms}
        self._last_tick: dict[str, float] = {sym: 0.0 for sym in syms}
        self._ws_task:   asyncio.Task | None = None
        self._rest_task: asyncio.Task | None = None
        self._running = False

    def supported_symbols(self) -> list[str]:
        return sorted(self._windows.keys())

    # ── Public API ────────────────────────────────────────────

    def window(self, symbol: str) -> list[tuple[float, float]]:
        """
        Return a snapshot of (epoch_ts, price) pairs within the last 5 minutes.
        Result is a plain list — safe to iterate without holding any lock.
        """
        now = time.time()
        cutoff = now - WINDOW_SECONDS
        dq = self._windows.get(symbol.upper(), deque())
        return [(ts, px) for ts, px in dq if ts >= cutoff]

    def is_fresh(self, symbol: str) -> bool:
        """Return True if we received a tick within STALE_THRESHOLD seconds."""
        last = self._last_tick.get(symbol.upper(), 0.0)
        return (time.time() - last) <= STALE_THRESHOLD

    def latest_price(self, symbol: str) -> float | None:
        """Most recent price for the symbol, or None if no data."""
        w = self.window(symbol)
        return w[-1][1] if w else None

    def price_at(self, symbol: str, epoch_ts: float, *, max_lookback_sec: float = 900.0) -> float | None:
        """
        Best-effort price lookup at or before epoch_ts.
        Returns None if we don't have a tick within max_lookback_sec.
        """
        dq = self._windows.get(symbol.upper())
        if not dq:
            return None
        lo = float(epoch_ts) - float(max_lookback_sec)
        for ts, px in reversed(dq):
            if ts <= epoch_ts:
                return px if ts >= lo else None
        return None

    def price_near(self, symbol: str, epoch_ts: float, *, tolerance_sec: float = 20.0) -> float | None:
        """
        Return the closest tick price within ±tolerance_sec of epoch_ts.
        Useful when tick cadence (REST polling) doesn't align perfectly with the target timestamp.
        """
        dq = self._windows.get(symbol.upper())
        if not dq:
            return None
        target = float(epoch_ts)
        tol = float(tolerance_sec)
        best_dt = None
        best_px = None
        for ts, px in dq:
            dt = abs(float(ts) - target)
            if dt <= tol and (best_dt is None or dt < best_dt):
                best_dt = dt
                best_px = px
        return best_px

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._ws_task   = asyncio.create_task(self._run_ws(),   name="spot-ws")
        self._rest_task = asyncio.create_task(self._run_rest(), name="spot-rest")
        log.info("SpotFeed: started (WS + REST fallback)")

    async def stop(self) -> None:
        self._running = False
        for task in (self._ws_task, self._rest_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        log.info("SpotFeed: stopped.")

    # ── REST fallback ─────────────────────────────────────────

    async def _run_rest(self) -> None:
        """Poll Coinbase public API every REST_POLL_INTERVAL seconds."""
        log.info("SpotFeed REST: polling Coinbase every %.0fs", REST_POLL_INTERVAL)
        async with aiohttp.ClientSession() as session:
            while self._running:
                for sym, cb_sym in COINBASE_SYMBOLS.items():
                    try:
                        url = COINBASE_REST.format(symbol=cb_sym)
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                            if resp.status == 200:
                                body = await resp.json()
                                price = float(body["data"]["amount"])
                                self._record(sym, price)
                    except asyncio.CancelledError:
                        return
                    except Exception as exc:
                        log.debug("SpotFeed REST %s error: %s", sym, exc)
                try:
                    await asyncio.sleep(REST_POLL_INTERVAL)
                except asyncio.CancelledError:
                    return

    # ── WebSocket primary ─────────────────────────────────────

    async def _run_ws(self) -> None:
        combined = "/".join(STREAMS.values())
        endpoints = [BINANCE_WS_US, BINANCE_WS_GLOBAL]
        ep_idx = 0

        while self._running:
            url = f"{endpoints[ep_idx % len(endpoints)]}?streams={combined}"
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    log.info("SpotFeed WS: connected to %s", url.split("?")[0])
                    async for raw in ws:
                        if not self._running:
                            break
                        self._handle_ws(raw)

            except asyncio.CancelledError:
                break
            except ConnectionClosed as exc:
                code = getattr(exc, "code", None)
                if code == 451:
                    ep_idx += 1
                    log.warning("SpotFeed WS: geo-blocked (451) — switching endpoint.")
                else:
                    log.warning("SpotFeed WS: closed (%s) — reconnecting in %.0fs", exc, RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)
            except Exception as exc:
                msg = str(exc)
                if "451" in msg:
                    ep_idx += 1
                    log.warning("SpotFeed WS: geo-blocked (451) — trying alternate endpoint.")
                else:
                    log.warning("SpotFeed WS: error (%s) — reconnecting in %.0fs", exc, RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)

    def _handle_ws(self, raw: str | bytes) -> None:
        """Parse a Binance stream message and record the price."""
        try:
            msg = json.loads(raw)
            stream: str = msg.get("stream", "")
            data: dict = msg.get("data", {})

            symbol: str | None = None
            for sym, stream_name in STREAMS.items():
                if stream_name.lower() in stream.lower():
                    symbol = sym
                    break
            if symbol is None:
                return

            price = float(data["p"])
            self._record(symbol, price)

        except (KeyError, ValueError, TypeError) as exc:
            log.debug("SpotFeed WS parse error: %s", exc)
        except Exception as exc:
            log.debug("SpotFeed WS unexpected parse error: %s", exc)

    # ── Internal ──────────────────────────────────────────────

    def _record(self, symbol: str, price: float) -> None:
        """Add a price tick to the rolling window."""
        ts = time.time()
        dq = self._windows.setdefault(symbol, deque())
        dq.append((ts, price))
        self._last_tick[symbol] = ts

        # Trim entries older than the window
        cutoff = ts - WINDOW_SECONDS
        while dq and dq[0][0] < cutoff:
            dq.popleft()
