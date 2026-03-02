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


def _spot_return_window(spot: SpotFeed, symbol: str, *, end_epoch: float) -> float | None:
    """
    Return spot return over the 5-minute window ending at end_epoch:
      ret = price(end_epoch) / price(end_epoch - 300) - 1
    """
    def _ret(end_ts: float) -> float | None:
        px_end = (
            spot.price_at(symbol, end_ts, max_lookback_sec=900.0)
            or spot.price_near(symbol, end_ts, tolerance_sec=20.0)
        )
        px_start = (
            spot.price_at(symbol, end_ts - 300.0, max_lookback_sec=900.0)
            or spot.price_near(symbol, end_ts - 300.0, tolerance_sec=20.0)
        )
        if px_end is None or px_start is None or px_start <= 0:
            return None
        return (float(px_end) - float(px_start)) / float(px_start)

    # Primary: align to the market end time.
    r = _ret(float(end_epoch))
    if r is not None:
        return r

    # Fallback: compute over the last 5 minutes ending "now" (close enough once end_epoch has passed).
    # This prevents trades from getting stuck UNRESOLVED due to timestamp/tick cadence misalignment.
    import time
    return _ret(time.time())


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

        end_epoch = float(t.get("end_epoch", 0.0))
        ret = _spot_return_window(spot, t["symbol"], end_epoch=end_epoch)
        if ret is None:
            # Retry briefly while end_epoch is still inside our rolling window.
            # If still missing after a grace period, drop as UNRESOLVED so open trades don't stick forever.
            if now < end_epoch + 30.0:
                STATE.open_trades[mid] = t
                continue

            if STATE.dry_run:
                STATE.dry_deployed = max(0.0, STATE.dry_deployed - float(t["size_usdc"]))

            STATE.resolved_trades.appendleft({
                "ts_epoch": now,
                "market_id": t["market_id"][:16],
                "symbol": t["symbol"],
                "side": t["side"],
                "size_usdc": round(float(t["size_usdc"]), 2),
                "price_paid": round(float(t["price_paid"]), 4),
                "outcome": "UNK",
                "spot_ret_5m_pct": None,
                "pnl_usdc": 0.0,
                "question": t["question"][:120],
                "note": "unresolved_missing_spot_history",
            })
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
