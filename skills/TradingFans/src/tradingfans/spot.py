"""
spot.py — Binance WebSocket spot price feed.

Subscribes to combined aggTrade streams for BTCUSDT and ETHUSDT.
Maintains a rolling 5-minute (300s) price window per symbol.

Public API:
    feed = SpotFeed()
    await feed.start()

    window = feed.window("BTC")      # list[(monotonic_ts, price)] last 5 min
    fresh  = feed.is_fresh("BTC")    # True if last tick was ≤ 2s ago
    price  = feed.latest_price("BTC")
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Deque

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

# Binance combined stream endpoint
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"

STREAMS: dict[str, str] = {
    "BTC": "btcusdt@aggTrade",
    "ETH": "ethusdt@aggTrade",
}

WINDOW_SECONDS = 300   # 5-minute rolling window
STALE_THRESHOLD = 2.0  # seconds before spot is considered stale
RECONNECT_DELAY = 3.0  # seconds between reconnect attempts


class SpotFeed:
    """Async Binance WebSocket feed with 5-minute rolling price windows."""

    def __init__(self) -> None:
        self._windows: dict[str, Deque[tuple[float, float]]] = {
            sym: deque() for sym in STREAMS
        }
        self._last_tick: dict[str, float] = {sym: 0.0 for sym in STREAMS}
        self._task: asyncio.Task | None = None
        self._running = False

    # ── Public API ────────────────────────────────────────────

    def window(self, symbol: str) -> list[tuple[float, float]]:
        """
        Return a snapshot of (monotonic_ts, price) pairs within the last 5 minutes.
        Result is a plain list — safe to iterate without holding any lock.
        """
        now = time.monotonic()
        cutoff = now - WINDOW_SECONDS
        dq = self._windows.get(symbol.upper(), deque())
        return [(ts, px) for ts, px in dq if ts >= cutoff]

    def is_fresh(self, symbol: str) -> bool:
        """Return True if we received a tick within STALE_THRESHOLD seconds."""
        last = self._last_tick.get(symbol.upper(), 0.0)
        return (time.monotonic() - last) <= STALE_THRESHOLD

    def latest_price(self, symbol: str) -> float | None:
        """Most recent price for the symbol, or None if no data."""
        w = self.window(symbol)
        return w[-1][1] if w else None

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="spot-feed")
        log.info("SpotFeed: started — subscribing to %s", list(STREAMS.values()))

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("SpotFeed: stopped.")

    # ── Internal ──────────────────────────────────────────────

    async def _run(self) -> None:
        combined = "/".join(STREAMS.values())
        url = f"{BINANCE_WS_URL}?streams={combined}"

        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    log.info("SpotFeed: connected to Binance WebSocket.")
                    async for raw in ws:
                        if not self._running:
                            break
                        self._handle(raw)

            except asyncio.CancelledError:
                break
            except ConnectionClosed as exc:
                log.warning("SpotFeed: connection closed (%s) — reconnecting in %.0fs", exc, RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)
            except Exception as exc:
                log.warning("SpotFeed: unexpected error (%s) — reconnecting in %.0fs", exc, RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)

    def _handle(self, raw: str | bytes) -> None:
        """Parse a Binance stream message and update the rolling window."""
        try:
            msg = json.loads(raw)
            stream: str = msg.get("stream", "")
            data: dict = msg.get("data", {})

            # Map stream name → symbol
            symbol: str | None = None
            for sym, stream_name in STREAMS.items():
                if stream_name in stream:
                    symbol = sym
                    break
            if symbol is None:
                return

            # aggTrade price is in field "p"
            price = float(data["p"])
            ts = time.monotonic()

            dq = self._windows[symbol]
            dq.append((ts, price))
            self._last_tick[symbol] = ts

            # Trim entries older than the window
            cutoff = ts - WINDOW_SECONDS
            while dq and dq[0][0] < cutoff:
                dq.popleft()

        except (KeyError, ValueError, TypeError) as exc:
            log.debug("SpotFeed: parse error: %s", exc)
        except Exception as exc:
            log.debug("SpotFeed: unexpected parse error: %s", exc)
