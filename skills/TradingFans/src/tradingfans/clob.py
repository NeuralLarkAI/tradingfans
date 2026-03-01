"""
clob.py — py-clob-client wrapper for Polymarket CLOB.

Provides:
  ClobClient.get_book(token_id)   -> OrderBook | None
  ClobClient.get_mid(token_id)    -> float | None   (implied YES probability)
  ClobClient.place_order(...)     -> str | None      (order_id)

Guardrail helpers (pure, no side effects):
  check_spread(book)              -> (ok: bool, spread: float)
  check_depth(book, mid)          -> bool
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from py_clob_client.client import ClobClient as _ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.constants import POLYGON

# In py-clob-client >= 0.20, Side is a plain string constant
BUY = "BUY"

log = logging.getLogger(__name__)

MAX_SPREAD = 0.03       # 3% maximum allowed spread
MIN_DEPTH_USDC = 100.0  # $100 minimum notional depth within 1.5% of mid
DEPTH_BAND_PCT = 0.015  # ±1.5% from mid counts as "near mid"


# ── Data model ────────────────────────────────────────────────

@dataclass
class PriceLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    bids: list[PriceLevel]   # sorted best (highest) first
    asks: list[PriceLevel]   # sorted best (lowest) first
    mid: float
    spread: float

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None


# ── Client factory ────────────────────────────────────────────

def _make_clob_client() -> _ClobClient:
    private_key = os.environ["POLY_PRIVATE_KEY"].strip()
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    chain_id = int(os.environ.get("POLY_CHAIN_ID", str(POLYGON)))
    funder = os.environ["POLY_FUNDER"].strip()

    client = _ClobClient(
        host="https://clob.polymarket.com",
        key=private_key,
        chain_id=chain_id,
        funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client


def _parse_levels(raw_levels: list) -> list[PriceLevel]:
    """
    Parse orderbook levels from py-clob-client.
    Handles both dict-form {"price": x, "size": y} and
    object-form with .price / .size attributes.
    """
    out: list[PriceLevel] = []
    for lvl in raw_levels or []:
        try:
            if isinstance(lvl, dict):
                p, s = float(lvl["price"]), float(lvl["size"])
            else:
                p, s = float(lvl.price), float(lvl.size)
            out.append(PriceLevel(price=p, size=s))
        except (KeyError, AttributeError, TypeError, ValueError) as exc:
            log.debug("Skipping malformed level: %s", exc)
    return out


# ── ClobClient ────────────────────────────────────────────────

class ClobClient:
    """Thin async-friendly wrapper around py_clob_client.ClobClient."""

    def __init__(self) -> None:
        self._client = _make_clob_client()
        log.info("ClobClient: authenticated against Polymarket CLOB.")

    # ── Read ──────────────────────────────────────────────────

    def get_book(self, token_id: str) -> OrderBook | None:
        """
        Fetch the live order book for a token.
        Returns None on any error or if book is empty.
        """
        try:
            raw = self._client.get_order_book(token_id)
            bids = _parse_levels(raw.bids)
            asks = _parse_levels(raw.asks)

            if not bids or not asks:
                return None

            # bids are highest-price first, asks lowest-price first
            best_bid = bids[0].price
            best_ask = asks[0].price

            if best_ask <= best_bid:
                # Crossed book — skip
                return None

            mid = (best_bid + best_ask) / 2.0
            spread = best_ask - best_bid

            return OrderBook(bids=bids, asks=asks, mid=mid, spread=spread)

        except Exception as exc:
            log.warning("get_book(%s…) failed: %s", token_id[:12], exc)
            return None

    def get_mid(self, token_id: str) -> float | None:
        """Return midpoint price (implied YES probability) or None."""
        book = self.get_book(token_id)
        return book.mid if book else None

    # ── Write ─────────────────────────────────────────────────

    def place_order(
        self,
        token_id: str,
        side_label: str,   # "BUY_YES" or "BUY_NO" for logging
        size_usdc: float,
        price: float,
    ) -> str | None:
        """
        Place a GTC limit buy order.
        Returns the order_id string on success, None on failure.
        Respects POLY_DRY_RUN=1 to skip actual submission.
        """
        dry_run = os.environ.get("POLY_DRY_RUN", "0") == "1"

        if dry_run:
            log.info(
                "DRY RUN | %s token=%s… size=%.2f USDC price=%.4f",
                side_label, token_id[:12], size_usdc, price,
            )
            return "dry-run"

        try:
            order_args = OrderArgs(
                price=price,
                size=size_usdc,
                side=BUY,
                token_id=token_id,
            )
            resp = self._client.create_and_post_order(order_args)

            # py-clob-client returns a dict; extract the order id
            order_id = (
                resp.get("orderID")
                or resp.get("orderId")
                or resp.get("id")
                or str(resp)
            )
            log.info(
                "ORDER PLACED | id=%s | %s | size=%.2f | price=%.4f",
                order_id, side_label, size_usdc, price,
            )
            return order_id

        except Exception as exc:
            log.error("place_order failed [%s]: %s", side_label, exc)
            return None


# ── Guardrail helpers (pure functions) ────────────────────────

def check_spread(book: OrderBook) -> tuple[bool, float]:
    """Return (passes, spread_value). Passes if spread ≤ MAX_SPREAD."""
    ok = book.spread <= MAX_SPREAD
    return ok, book.spread


def check_depth(
    book: OrderBook,
    mid: float,
    min_notional: float = MIN_DEPTH_USDC,
) -> bool:
    """
    Return True if both bid-side and ask-side notional within ±DEPTH_BAND_PCT
    of mid are each ≥ min_notional.
    """
    band = mid * DEPTH_BAND_PCT
    lo, hi = mid - band, mid + band

    bid_notional = sum(
        lvl.price * lvl.size for lvl in book.bids if lvl.price >= lo
    )
    ask_notional = sum(
        lvl.price * lvl.size for lvl in book.asks if lvl.price <= hi
    )
    return min(bid_notional, ask_notional) >= min_notional
