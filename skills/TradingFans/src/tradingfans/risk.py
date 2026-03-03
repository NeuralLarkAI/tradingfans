"""
risk.py — Guardrail layer.

ALL guardrails must pass before an order can be submitted.
This is the last line of defense before capital is committed.

Guardrails (in evaluation order):
  1. time_to_expiry must be in (0, max_time_to_expiry) seconds
  2. Spot price must be fresh (last tick ≤ STALE_THRESHOLD ago)
  3. Order book must be available  [skipped in dry-run]
  4. Spread ≤ 3%                   [skipped in dry-run]
  5. Depth ≥ $100 notional         [skipped in dry-run]

In dry-run mode (POLY_DRY_RUN=1), CLOB spread/depth checks are bypassed
because no real orders are placed, so fill quality is irrelevant.

Usage:
    from .risk import evaluate, RiskCheck
    rc = evaluate(time_to_expiry=120.0, spot_fresh=True, book=book, implied_yes=0.3)
    if not rc.ok:
        log.debug("Risk FAIL: %s", rc.reasons)
        return
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .clob import OrderBook, check_depth, check_spread

# ── Thresholds ────────────────────────────────────────────────
MIN_TIME_TO_EXPIRY = 0.0    # seconds (must be > 0, i.e., not expired)
MAX_TIME_TO_EXPIRY = 600.0  # seconds (10-minute cap)


# ── Result ────────────────────────────────────────────────────

@dataclass
class RiskCheck:
    ok: bool = True
    reasons: list[str] = field(default_factory=list)

    def fail(self, reason: str) -> "RiskCheck":
        """Mark as failed and record the reason. Returns self for chaining."""
        self.ok = False
        self.reasons.append(reason)
        return self

    def summary(self) -> str:
        if self.ok:
            return "PASS"
        return "FAIL: " + " | ".join(self.reasons)


# ── Main evaluator ────────────────────────────────────────────

def evaluate(
    time_to_expiry: float,
    spot_fresh: bool,
    book: OrderBook | None,
    implied_yes: float,
    *,
    max_time_to_expiry: float = MAX_TIME_TO_EXPIRY,
    allow_wide_book_live: bool = False,
) -> RiskCheck:
    """
    Run all guardrails and return a RiskCheck.

    Args:
        time_to_expiry: Seconds until market closes (from Market.time_to_expiry).
        spot_fresh:     Whether spot feed has ticked within STALE_THRESHOLD.
        book:           Live CLOB order book (None if unavailable).
        implied_yes:    Mid price of YES token (0–1).

    Returns:
        RiskCheck.ok == True only if ALL guardrails pass.
    """
    dry_run = os.environ.get("POLY_DRY_RUN", "0") == "1"
    rc = RiskCheck()

    # 1. Time filter: only trade shortly before expiry
    if not (MIN_TIME_TO_EXPIRY < time_to_expiry < max_time_to_expiry):
        rc.fail(
            f"time_to_expiry={time_to_expiry:.1f}s not in "
            f"({MIN_TIME_TO_EXPIRY}, {max_time_to_expiry})"
        )

    # 2. Spot freshness
    if not spot_fresh:
        rc.fail("Spot price stale")

    # 3–5. Order book checks
    # - Always skipped in dry run.
    # - In LIVE, they can be explicitly bypassed for 5m "wide book" markets (opt-in).
    if not dry_run and not allow_wide_book_live:
        if book is None:
            rc.fail("Order book unavailable")
        else:
            # 4. Spread
            spread_ok, spread_val = check_spread(book)
            if not spread_ok:
                rc.fail(f"Spread {spread_val:.4f} exceeds max {0.03:.2f}")

            # 5. Depth
            if not check_depth(book, book.mid):
                rc.fail("Insufficient depth near mid (< $100 USDC notional)")

    return rc
