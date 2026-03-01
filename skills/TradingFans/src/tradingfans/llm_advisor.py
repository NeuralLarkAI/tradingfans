"""
llm_advisor.py — Asynchronous post-trade LLM commentary.

⚠️  The LLM is NEVER in the trade-execution loop.
    It runs in a background worker AFTER a trade is logged,
    purely for audit, commentary, and market regime analysis.

Provides:
    LLMAdvisor.queue_trade(event)       — non-blocking; fire and forget
    LLMAdvisor.regime_summary(recent)   — async; returns a string
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

import openai

log = logging.getLogger(__name__)

_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
_MAX_QUEUE = 50   # drop commentary if queue is full (never block trader)

_TRADE_PROMPT = """\
You are a quantitative trading analyst reviewing a completed Polymarket trade.
Provide a concise (2–3 sentences) post-hoc analysis of the trade and the market
conditions that justified it. Be factual and brief. Do NOT recommend future trades.

Trade data:
{trade_json}
"""

_REGIME_PROMPT = """\
You are a market microstructure analyst reviewing recent automated trading decisions.
Summarise the trading regime based on the data below. Focus on:
  - Average edge quality and signal strength
  - Market liquidity and spread environment
  - Momentum character (trending vs mean-reverting)
Keep the summary to 4 sentences or fewer.

Recent decisions (last 20):
{decisions_json}
"""


@dataclass
class TradeEvent:
    market_id:  str
    question:   str
    signal:     str     # "BUY_YES" or "BUY_NO"
    p_model:    float
    implied_yes: float
    edge:       float
    m1:         float
    m5:         float
    vol1:       float
    size_usdc:  float
    order_id:   str | None
    timestamp:  str     # ISO-8601 UTC


class LLMAdvisor:
    """
    Background worker that generates post-trade commentary via OpenAI.
    Lives alongside the trading engine but never delays order placement.
    """

    def __init__(self) -> None:
        self._client = openai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self._queue: asyncio.Queue[TradeEvent] = asyncio.Queue(maxsize=_MAX_QUEUE)
        self._task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────

    async def start(self) -> None:
        self._task = asyncio.create_task(self._worker(), name="llm-advisor")
        log.info("LLMAdvisor: background worker started (model=%s).", _MODEL)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        log.info("LLMAdvisor: stopped.")

    # ── Public API ────────────────────────────────────────────

    def queue_trade(self, event: TradeEvent) -> None:
        """
        Enqueue a trade event for async commentary.
        Non-blocking: if queue is full the event is dropped (logged at DEBUG).
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.debug(
                "LLMAdvisor: queue full — dropping commentary for market %s",
                event.market_id[:12],
            )

    async def regime_summary(self, recent_decisions: list[dict]) -> str:
        """
        Generate a regime summary from a list of recent decision dicts.
        Returns a string even on failure (never raises).
        """
        if not recent_decisions:
            return "No recent decisions to summarise."
        try:
            resp = await self._client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": _REGIME_PROMPT.format(
                            decisions_json=json.dumps(
                                recent_decisions[-20:], indent=2
                            )
                        ),
                    }
                ],
                max_tokens=300,
                temperature=0.3,
            )
            return resp.choices[0].message.content or "No summary returned."
        except Exception as exc:
            log.warning("LLMAdvisor.regime_summary failed: %s", exc)
            return f"LLM unavailable: {exc}"

    # ── Internal ──────────────────────────────────────────────

    async def _worker(self) -> None:
        """Drain the queue and generate commentary for each trade event."""
        while True:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                await self._narrate(event)
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("LLMAdvisor worker error: %s", exc)

    async def _narrate(self, event: TradeEvent) -> None:
        """Call OpenAI for a short trade commentary and log it."""
        trade_json = json.dumps(
            {
                "question":    event.question,
                "signal":      event.signal,
                "p_model":     round(event.p_model, 4),
                "implied_yes": round(event.implied_yes, 4),
                "edge":        round(event.edge, 4),
                "m1_momentum": round(event.m1, 6),
                "m5_momentum": round(event.m5, 6),
                "vol_1m":      round(event.vol1, 6),
                "size_usdc":   event.size_usdc,
                "order_id":    event.order_id,
                "timestamp":   event.timestamp,
            },
            indent=2,
        )
        try:
            resp = await self._client.chat.completions.create(
                model=_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": _TRADE_PROMPT.format(trade_json=trade_json),
                    }
                ],
                max_tokens=200,
                temperature=0.3,
            )
            commentary = (resp.choices[0].message.content or "").strip()
            log.info(
                "📝 LLM | market=%s | %s",
                event.market_id[:16],
                commentary,
            )
        except Exception as exc:
            log.warning("LLMAdvisor._narrate failed: %s", exc)
