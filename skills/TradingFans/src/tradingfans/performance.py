"""
performance.py — Realized outcome + PnL tracking for 5-minute crypto markets.

For these markets, we approximate resolution as:
  UP  if spot_end > spot_start over the 5-minute window ending at market end_time
  DOWN otherwise

This is intended for DRY RUN learning and calibration, not authoritative settlement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .spot import SpotFeed
from .state import STATE


@dataclass(frozen=True)
class OpenTrade:
    market_id: str
    question: str
    symbol: str
    side: str              # BUY_YES | BUY_NO
    size_usdc: float
    price_paid: float
    entry_epoch: float
    end_epoch: float


def record_open_trade(t: OpenTrade) -> None:
    STATE.open_trades[t.market_id] = {
        "market_id": t.market_id,
        "question": t.question,
        "symbol": t.symbol,
        "side": t.side,
        "size_usdc": float(t.size_usdc),
        "price_paid": float(t.price_paid),
        "entry_epoch": float(t.entry_epoch),
        "end_epoch": float(t.end_epoch),
    }


def _pnl_usdc(*, side: str, size_usdc: float, price_paid: float, outcome_up: bool) -> float:
    """
    Approximate realized PnL for a buy on a binary token.
    Spending `size_usdc` at price p buys shares = size/p; payout if win is shares*1.
    """
    p = max(0.001, min(0.999, float(price_paid)))
    win = outcome_up if side == "BUY_YES" else (not outcome_up)
    if not win:
        return -float(size_usdc)
    return float(size_usdc) * (1.0 / p - 1.0)


def _spot_return_over_last_5m(spot: SpotFeed, symbol: str) -> float | None:
    w = spot.window(symbol)
    if len(w) < 2:
        return None
    now_ts, px_now = w[-1]
    cutoff = now_ts - 300.0
    px_old = None
    for ts, px in reversed(w):
        if ts <= cutoff:
            px_old = px
            break
    if px_old is None or px_old <= 0:
        return None
    return (px_now - px_old) / px_old


def resolve_due_trades(spot: SpotFeed) -> None:
    """
    Resolve any open trades whose end_epoch has passed and update STATE PnL + history.
    """
    now = time.time()
    due = [mid for mid, t in STATE.open_trades.items() if now >= float(t.get("end_epoch", 0)) + 2.0]
    for mid in due:
        t = STATE.open_trades.pop(mid, None)
        if not t:
            continue

        ret = _spot_return_over_last_5m(spot, t["symbol"])
        if ret is None:
            # If we can't compute, keep it open and retry.
            STATE.open_trades[mid] = t
            continue

        outcome_up = ret > 0
        pnl = _pnl_usdc(
            side=t["side"],
            size_usdc=float(t["size_usdc"]),
            price_paid=float(t["price_paid"]),
            outcome_up=outcome_up,
        )

        # Release deployed capital and book realized PnL (dry-run only).
        if STATE.dry_run:
            STATE.dry_deployed = max(0.0, STATE.dry_deployed - float(t["size_usdc"]))
            STATE.dry_realized_pnl += pnl

        STATE.resolved_trades.appendleft({
            "ts_epoch": now,
            "market_id": t["market_id"][:16],
            "symbol": t["symbol"],
            "side": t["side"],
            "size_usdc": round(float(t["size_usdc"]), 2),
            "price_paid": round(float(t["price_paid"]), 4),
            "outcome": "UP" if outcome_up else "DOWN",
            "spot_ret_5m_pct": round(ret * 100, 3),
            "pnl_usdc": round(pnl, 2),
            "question": t["question"][:120],
        })

