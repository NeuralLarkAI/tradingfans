"""
engine.py — Main asyncio trading engine.

Orchestration order per poll cycle:
  1. GammaCache → get active 5-minute crypto markets
  2. For each market:
     a. Identify symbol (BTC/ETH) from question text
     b. Fetch live order book via ClobClient (run in executor → non-blocking)
     c. Run all RiskCheck guardrails
     d. Run DecisionEngine to compute p_model, edge, signal
     e. If signal ≠ NO_TRADE → size the order and call ClobClient.place_order
     f. Queue a TradeEvent for async LLM commentary (never blocks)
  3. Sleep POLL_INTERVAL seconds, repeat.

Entry:
    python -m tradingfans.engine [--dry-run] [--max-size N] [--symbol BTC|ETH|both]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os
import signal
import sys
from collections import deque
from typing import Deque

from .clob import ClobClient
from .decision import DecisionResult, compute as decision_compute
from .gamma import GammaCache, Market
from .llm_advisor import LLMAdvisor, TradeEvent
from .risk import RiskCheck, evaluate as risk_evaluate
from .spot import SpotFeed

# ── Logging setup ─────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s %(levelname)-7s %(name)-18s — %(message)s",
    datefmt="%H:%M:%S",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("engine")

# ── Config (runtime, from env) ────────────────────────────────
MAX_SIZE_USDC   = float(os.environ.get("POLY_MAX_SIZE", "10"))
POLL_INTERVAL   = float(os.environ.get("POLY_POLL_INTERVAL", "5"))
SYMBOL_FILTER   = os.environ.get("POLY_SYMBOL", "both").upper()  # BTC | ETH | BOTH

# Max trade size per market (position guard — prevents double-entry on repeat polls)
_open_positions: set[str] = set()

# Rolling log of recent decisions for LLM regime summary (max 200 entries)
_decision_log: Deque[dict] = deque(maxlen=200)


# ── Helpers ───────────────────────────────────────────────────

def _detect_symbol(market: Market) -> str | None:
    """
    Infer BTC or ETH from market question / slug.
    Returns None if we can't tell.
    """
    text = (market.question + " " + market.slug).lower()
    if "btc" in text or "bitcoin" in text:
        return "BTC"
    if "eth" in text or "ethereum" in text:
        return "ETH"
    return None


def _size_usdc(edge: float, max_size: float) -> float:
    """
    Scale order size linearly with edge conviction.
      edge=0.02 → 10% of max_size
      edge=0.10 → 50% of max_size
      edge≥0.20 → 100% of max_size
    Clamped to [0, max_size] and rounded to 2 dp.
    """
    fraction = min(abs(edge) / 0.20, 1.0)   # 0.0 – 1.0
    raw = fraction * max_size
    return round(max(raw, 0.0), 2)


def _utc_now_iso() -> str:
    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat()


# ── Per-market evaluation (called concurrently) ───────────────

async def _evaluate_market(
    market: Market,
    spot: SpotFeed,
    clob: ClobClient,
    max_size: float,
    symbol_filter: str,
    loop: asyncio.AbstractEventLoop,
) -> tuple[Market, DecisionResult, float, str | None] | None:
    """
    Evaluate a single market and, if all checks pass, place an order.

    Returns (market, decision, size, order_id) on a triggered trade,
    or None if the market was skipped or no trade was warranted.
    """
    # ── Skip if we already hold a position ────────────────────
    if market.market_id in _open_positions:
        return None

    # ── Symbol filter ─────────────────────────────────────────
    symbol = _detect_symbol(market)
    if symbol is None:
        return None
    if symbol_filter not in ("BOTH", symbol):
        return None

    # ── Spot data ─────────────────────────────────────────────
    spot_fresh = spot.is_fresh(symbol)
    window = spot.window(symbol)

    # ── Order book (run in executor to keep event loop free) ──
    book = await loop.run_in_executor(None, clob.get_book, market.yes_token_id)
    implied_yes: float | None = book.mid if book else None
    if implied_yes is None:
        return None

    # ── Risk guardrails ───────────────────────────────────────
    rc: RiskCheck = risk_evaluate(
        time_to_expiry=market.time_to_expiry,
        spot_fresh=spot_fresh,
        book=book,
        implied_yes=implied_yes,
    )
    if not rc.ok:
        log.debug(
            "RISK FAIL | %s… | %s",
            market.market_id[:12],
            " | ".join(rc.reasons),
        )
        return None

    # ── Decision ──────────────────────────────────────────────
    decision = decision_compute(window=window, implied_yes=implied_yes)

    log.debug(
        "SIGNAL=%s | tte=%.0fs | impl=%.3f | model=%.3f | edge=%+.4f | %s…",
        decision.signal,
        market.time_to_expiry,
        implied_yes,
        decision.p_model,
        decision.edge,
        market.market_id[:12],
    )

    if decision.signal == "NO_TRADE":
        return None

    # ── Size the order ────────────────────────────────────────
    size = _size_usdc(decision.edge, max_size)
    if size < 0.50:
        log.debug("Order size %.2f USDC < minimum 0.50 — skipping.", size)
        return None

    # ── Select token + price ──────────────────────────────────
    if decision.signal == "BUY_YES":
        token_id = market.yes_token_id
        # Use best ask as the limit price (taker fill)
        price = book.best_ask if book.best_ask else implied_yes
    else:  # BUY_NO
        token_id = market.no_token_id
        # NO token price is approximately (1 - YES_ask)
        price = 1.0 - (book.best_bid if book.best_bid else implied_yes)
        price = max(0.01, min(price, 0.99))

    log.info(
        "🔔 TRADE | %-8s | size=%.2f USDC | price=%.4f | edge=%+.4f | %s",
        decision.signal,
        size,
        price,
        decision.edge,
        market.question[:55],
    )

    # ── Place order ───────────────────────────────────────────
    order_id = await loop.run_in_executor(
        None, clob.place_order, token_id, decision.signal, size, price
    )

    if order_id:
        _open_positions.add(market.market_id)

    return market, decision, size, order_id


# ── Main trading loop ─────────────────────────────────────────

async def trading_loop(
    gamma: GammaCache,
    spot: SpotFeed,
    clob: ClobClient,
    llm: LLMAdvisor,
    max_size: float,
    symbol_filter: str,
) -> None:
    loop = asyncio.get_running_loop()
    log.info(
        "Trading loop live | max_size=%.2f USDC | poll=%.1fs | symbol=%s",
        max_size, POLL_INTERVAL, symbol_filter,
    )

    while True:
        try:
            markets = await gamma.active_markets()

            if not markets:
                log.debug("No active 5m crypto markets found — waiting.")
            else:
                tasks = [
                    _evaluate_market(m, spot, clob, max_size, symbol_filter, loop)
                    for m in markets
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for market, result in zip(markets, results):
                    if isinstance(result, Exception):
                        log.warning(
                            "Market eval error [%s…]: %s",
                            market.market_id[:12],
                            result,
                        )
                    elif result is not None:
                        mkt, decision, size, order_id = result

                        # Log for regime analysis
                        _decision_log.append(
                            {
                                "ts":           _utc_now_iso(),
                                "market_id":    mkt.market_id[:16],
                                "question":     mkt.question[:80],
                                "signal":       decision.signal,
                                "edge":         round(decision.edge, 4),
                                "p_model":      round(decision.p_model, 4),
                                "implied_yes":  round(decision.p_model - decision.edge, 4),
                                "m1":           round(decision.m1, 6),
                                "m5":           round(decision.m5, 6),
                                "vol1":         round(decision.vol1, 6),
                                "size_usdc":    size,
                                "order_id":     order_id,
                            }
                        )

                        # Fire-and-forget LLM commentary
                        llm.queue_trade(
                            TradeEvent(
                                market_id=mkt.market_id,
                                question=mkt.question,
                                signal=decision.signal,
                                p_model=decision.p_model,
                                implied_yes=decision.p_model - decision.edge,
                                edge=decision.edge,
                                m1=decision.m1,
                                m5=decision.m5,
                                vol1=decision.vol1,
                                size_usdc=size,
                                order_id=order_id,
                                timestamp=_utc_now_iso(),
                            )
                        )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Trading loop error: %s", exc, exc_info=True)

        await asyncio.sleep(POLL_INTERVAL)


# ── Entry point ───────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="TradingFans — Polymarket 5-minute crypto trading agent"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Log trades without placing real orders (sets POLY_DRY_RUN=1)",
    )
    parser.add_argument(
        "--max-size", type=float, default=None,
        metavar="USDC",
        help=f"Max USDC per order (default: {MAX_SIZE_USDC})",
    )
    parser.add_argument(
        "--symbol", choices=["BTC", "ETH", "both"], default=None,
        help="Restrict to BTC, ETH, or both (default: both)",
    )
    args = parser.parse_args()

    # Apply CLI overrides to env
    if args.dry_run:
        os.environ["POLY_DRY_RUN"] = "1"
    if args.max_size is not None:
        os.environ["POLY_MAX_SIZE"] = str(args.max_size)
    if args.symbol is not None:
        os.environ["POLY_SYMBOL"] = args.symbol

    # Re-read config after overrides
    max_size = float(os.environ.get("POLY_MAX_SIZE", str(MAX_SIZE_USDC)))
    sym_filter = os.environ.get("POLY_SYMBOL", "both").upper()
    dry = os.environ.get("POLY_DRY_RUN", "0") == "1"

    log.info("=" * 60)
    log.info("TradingFans v1.0.0  |  Polymarket 5m Crypto Agent")
    log.info("  DRY RUN : %s", dry)
    log.info("  Max size: %.2f USDC", max_size)
    log.info("  Symbol  : %s", sym_filter)
    log.info("  Poll    : %.1fs", POLL_INTERVAL)
    log.info("=" * 60)

    gamma = GammaCache()
    spot  = SpotFeed()
    clob  = ClobClient()
    llm   = LLMAdvisor()

    # ── Graceful shutdown ─────────────────────────────────────
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal(sig: signal.Signals) -> None:
        log.info("Received %s — initiating graceful shutdown...", sig.name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, OSError):
            # Windows doesn't support add_signal_handler for all signals
            signal.signal(sig, lambda s, f: stop_event.set())

    # ── Start subsystems ──────────────────────────────────────
    await spot.start()
    await llm.start()

    log.info("Warming up spot feed (3s)...")
    await asyncio.sleep(3.0)   # let Binance WS connect and buffer first ticks

    # ── Launch main loop ──────────────────────────────────────
    trade_task = asyncio.create_task(
        trading_loop(gamma, spot, clob, llm, max_size, sym_filter),
        name="trading-loop",
    )

    await stop_event.wait()

    log.info("Shutting down...")
    trade_task.cancel()
    try:
        await trade_task
    except asyncio.CancelledError:
        pass

    await spot.stop()
    await llm.stop()

    if _decision_log:
        log.info("Session summary: %d trade(s) executed.", len(_decision_log))
        summary = await llm.regime_summary(list(_decision_log))
        log.info("Regime summary:\n%s", summary)

    log.info("TradingFans shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
