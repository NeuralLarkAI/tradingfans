"""
gamma.py — Polymarket Gamma API market discovery.

Polls https://gamma-api.polymarket.com/events every CACHE_TTL seconds.

Filters:
  - active=true, closed=false
  - tagged "Crypto"
  - "5-minute" OR "5m" in title / slug / question

Exposes:
  gamma = GammaCache()
  markets: list[Market] = await gamma.active_markets()
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com/events"
CACHE_TTL = 60  # seconds


# ── Data model ────────────────────────────────────────────────

@dataclass
class Market:
    market_id: str
    question: str
    end_time: datetime       # UTC-aware
    tick_size: float
    neg_risk: bool
    yes_token_id: str
    no_token_id: str
    slug: str = ""
    gamma_best_bid: float | None = None
    gamma_best_ask: float | None = None
    gamma_last_trade: float | None = None
    gamma_outcome_prices: list[float] | None = None

    @property
    def time_to_expiry(self) -> float:
        """Seconds until market closes (negative when expired)."""
        now = datetime.now(tz=timezone.utc)
        return (self.end_time - now).total_seconds()

    @property
    def gamma_mid(self) -> float | None:
        """
        Best-effort mid price from Gamma fields (YES price for the market).
        """
        if self.gamma_best_bid is not None and self.gamma_best_ask is not None and self.gamma_best_ask > self.gamma_best_bid:
            return (float(self.gamma_best_bid) + float(self.gamma_best_ask)) / 2.0
        if self.gamma_last_trade is not None:
            return float(self.gamma_last_trade)
        if self.gamma_outcome_prices and len(self.gamma_outcome_prices) >= 1:
            # Gamma returns outcomePrices for the market. For these 5m Up/Down events, the market is typically binary
            # and this value has historically aligned with the "YES" token price used by the CLOB.
            return float(self.gamma_outcome_prices[0])
        return None


# ── Filter helpers ────────────────────────────────────────────

def _is_five_minute(event: dict) -> bool:
    """Return True if the event or any of its markets looks like a 5-minute market."""
    candidates = [
        event.get("title", ""),
        event.get("slug", ""),
        event.get("description", ""),
    ]
    for market in event.get("markets", []):
        candidates.append(market.get("question", ""))
        candidates.append(market.get("slug", ""))

    for text in candidates:
        t = (text or "").lower()
        if "5-minute" in t or "5 minute" in t or "5m " in t or t.endswith("5m"):
            return True
    return False


def _is_crypto_tagged(event: dict) -> bool:
    tags = event.get("tags") or []
    for tag in tags:
        label = (tag.get("label") or tag.get("name") or "").lower()
        if label == "crypto":
            return True
    return False


# ── Parsing ───────────────────────────────────────────────────

def _parse_clob_token_ids(market: dict) -> tuple[str, str] | None:
    """Extract (yes_token_id, no_token_id) from a market dict."""
    raw = market.get("clobTokenIds")
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
    if isinstance(raw, list) and len(raw) >= 2:
        return str(raw[0]), str(raw[1])
    return None


def _parse_end_time(market: dict, event: dict) -> datetime | None:
    raw = market.get("endDate") or event.get("endDate")
    if not raw:
        return None
    try:
        # Handle both "Z" suffix and "+00:00" offset
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _parse_markets(event: dict) -> list[Market]:
    out: list[Market] = []
    for mkt in event.get("markets") or []:
        try:
            tokens = _parse_clob_token_ids(mkt)
            if tokens is None:
                continue

            end_time = _parse_end_time(mkt, event)
            if end_time is None:
                continue

            market_id = (
                mkt.get("id")
                or mkt.get("conditionId")
                or mkt.get("marketMakerAddress")
                or ""
            )
            if not market_id:
                continue

            # Optional pricing fields from Gamma (can help when CLOB books are pathological).
            best_bid = mkt.get("bestBid")
            best_ask = mkt.get("bestAsk")
            last_trade = mkt.get("lastTradePrice")
            outcome_prices_raw = mkt.get("outcomePrices")
            outcome_prices: list[float] | None = None
            try:
                if isinstance(outcome_prices_raw, str):
                    outcome_prices_raw = json.loads(outcome_prices_raw)
                if isinstance(outcome_prices_raw, list):
                    outcome_prices = [float(x) for x in outcome_prices_raw]
            except Exception:
                outcome_prices = None

            out.append(
                Market(
                    market_id=str(market_id),
                    question=mkt.get("question") or event.get("title") or "",
                    end_time=end_time,
                    tick_size=float(mkt.get("tickSize") or 0.01),
                    neg_risk=bool(mkt.get("negRisk")),
                    yes_token_id=tokens[0],
                    no_token_id=tokens[1],
                    slug=mkt.get("slug") or event.get("slug") or "",
                    gamma_best_bid=float(best_bid) if best_bid is not None else None,
                    gamma_best_ask=float(best_ask) if best_ask is not None else None,
                    gamma_last_trade=float(last_trade) if last_trade is not None else None,
                    gamma_outcome_prices=outcome_prices,
                )
            )
        except Exception as exc:
            log.debug("Skipping malformed market entry: %s", exc)
    return out


# ── Fetch ─────────────────────────────────────────────────────

def _fetch_markets() -> list[Market]:
    """Synchronous fetch — run in executor to avoid blocking event loop."""
    now_iso = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    resp = requests.get(
        GAMMA_URL,
        params={
            "active": "true",
            "closed": "false",
            "tag_slug": "5m",
            "end_date_min": now_iso,
            "limit": "100",
        },
        timeout=12,
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()

    body = resp.json()
    # API may return {"events": [...]} or a bare list
    if isinstance(body, dict):
        events = body.get("events") or body.get("data") or []
    else:
        events = body

    markets: list[Market] = []
    for event in events:
        markets.extend(_parse_markets(event))

    return markets


# ── Cache ─────────────────────────────────────────────────────

class GammaCache:
    """Thread-safe async cache over the Gamma API."""

    def __init__(self) -> None:
        self._markets: list[Market] = []
        self._last_refresh: float = 0.0
        self._lock = asyncio.Lock()

    async def active_markets(self) -> list[Market]:
        """Return all currently active 5-minute crypto markets."""
        async with self._lock:
            if time.monotonic() - self._last_refresh >= CACHE_TTL:
                await self._refresh()
        return list(self._markets)

    async def _refresh(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            markets = await loop.run_in_executor(None, _fetch_markets)
            self._markets = markets
            self._last_refresh = time.monotonic()
            log.info(
                "GammaCache: refreshed — %d active 5m crypto market(s)", len(markets)
            )
        except Exception as exc:
            log.error("GammaCache refresh failed: %s", exc)
            # Keep stale data; don't reset last_refresh so next call retries
