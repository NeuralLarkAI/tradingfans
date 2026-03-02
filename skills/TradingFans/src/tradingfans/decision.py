"""
decision.py — Pure-quant decision engine.

⚠️  NO LLM. No network calls. No side effects.
    This module is a pure function: same inputs → same output.

Inputs:
    window      : list[(monotonic_ts, price)]  — rolling 5-minute price history
    implied_yes : float                         — mid from CLOB order book

Output:
    DecisionResult with signal ∈ {"BUY_YES", "BUY_NO", "NO_TRADE"}

Model:
    m1     = 1-minute price momentum  (returns, signed)
    m5     = 5-minute price momentum
    vol1   = 1-minute log-return std-dev  (unsigned)

    logit  = W_M1*m1 + W_M5*m5 + W_VOL*vol1
    p_model = sigmoid(logit)           # P(YES wins | spot data)
    edge    = p_model - implied_yes    # alpha vs market

    signal:
      abs(edge) < MIN_EDGE            → NO_TRADE
      implied_yes in [0.45, 0.55]     → NO_TRADE  (hard no-trade zone)
      edge ≥  MIN_EDGE                → BUY_YES
      edge ≤ -MIN_EDGE                → BUY_NO
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ── Hyper-parameters ──────────────────────────────────────────
#
# Weights calibrated for 5-minute binary crypto markets.
# 1m momentum is the primary signal; 5m provides trend context.
# High volatility pulls p_model toward 0.5 (reduces conviction).

W_M1  = 20.0   # 1m momentum weight
W_M5  =  8.0   # 5m momentum weight
W_VOL = -4.0   # vol penalty (high vol → less edge)

MIN_EDGE = 0.02     # minimum |edge| to consider trading


# ── Result ────────────────────────────────────────────────────

@dataclass(frozen=True)
class DecisionResult:
    p_model:    float    # model's P(YES wins)
    edge:       float    # p_model - implied_yes
    m1:         float    # 1-minute momentum
    m5:         float    # 5-minute momentum
    vol1:       float    # 1-minute return volatility
    signal:     str      # "BUY_YES" | "BUY_NO" | "NO_TRADE"
    confidence: float    # abs(edge)
    reason:     str      # human-readable explanation


# ── Math helpers ──────────────────────────────────────────────

def _sigmoid(x: float) -> float:
    x = max(-20.0, min(20.0, x))   # clamp to avoid overflow
    return 1.0 / (1.0 + math.exp(-x))


def _momentum(
    window: list[tuple[float, float]],
    lookback_sec: float,
) -> float | None:
    """
    Return (price_now - price_T) / price_T over lookback_sec seconds.
    Returns None if insufficient history.
    """
    if len(window) < 2:
        return None
    now_ts, price_now = window[-1]
    cutoff = now_ts - lookback_sec
    # Find the last tick at or before the cutoff
    old_price: float | None = None
    for ts, px in reversed(window):
        if ts <= cutoff:
            old_price = px
            break
    if old_price is None or old_price == 0.0:
        return None
    return (price_now - old_price) / old_price


def _vol_1m(window: list[tuple[float, float]]) -> float | None:
    """
    Std-deviation of log-returns over the last 60 seconds.
    Returns None if insufficient history (< 2 ticks).
    """
    if len(window) < 2:
        return None
    now_ts = window[-1][0]
    cutoff = now_ts - 60.0
    recent = [px for ts, px in window if ts >= cutoff and px > 0.0]
    if len(recent) < 2:
        return None
    log_rets = [
        math.log(recent[i] / recent[i - 1])
        for i in range(1, len(recent))
        if recent[i - 1] > 0.0
    ]
    if not log_rets:
        return None
    mean = sum(log_rets) / len(log_rets)
    var  = sum((r - mean) ** 2 for r in log_rets) / len(log_rets)
    return math.sqrt(var)


# ── Main entry point ──────────────────────────────────────────

def compute(
    window: list[tuple[float, float]],
    implied_yes: float,
    *,
    min_edge: float = MIN_EDGE,
    w_m1: float = W_M1,
    w_m5: float = W_M5,
    w_vol: float = W_VOL,
) -> DecisionResult:
    """
    Compute a trading signal from price history and CLOB implied probability.

    Args:
        window:       Rolling 5m price history as (monotonic_ts, price) pairs.
        implied_yes:  Mid price of YES token from CLOB (0–1 scale).

    Returns:
        DecisionResult with signal and supporting analytics.
    """
    # ── Compute features ──────────────────────────────────────
    m1   = _momentum(window, 60.0)  or 0.0
    m5   = _momentum(window, 300.0) or 0.0
    vol1 = _vol_1m(window)          or 0.0

    # ── Logistic model ────────────────────────────────────────
    logit   = w_m1 * m1 + w_m5 * m5 + w_vol * vol1
    p_model = _sigmoid(logit)
    edge    = p_model - implied_yes

    # ── Signal ────────────────────────────────────────────────
    if abs(edge) < min_edge:
        signal = "NO_TRADE"
        reason = f"Edge {edge:+.4f} < threshold {min_edge}"
    elif edge > 0:
        signal = "BUY_YES"
        reason = (
            f"BUY YES — p_model={p_model:.3f} implied={implied_yes:.3f} "
            f"edge={edge:+.4f} m1={m1:.5f} m5={m5:.5f} vol1={vol1:.5f}"
        )
    else:
        signal = "BUY_NO"
        reason = (
            f"BUY NO  — p_model={p_model:.3f} implied={implied_yes:.3f} "
            f"edge={edge:+.4f} m1={m1:.5f} m5={m5:.5f} vol1={vol1:.5f}"
        )

    return DecisionResult(
        p_model=p_model,
        edge=edge,
        m1=m1,
        m5=m5,
        vol1=vol1,
        signal=signal,
        confidence=abs(edge),
        reason=reason,
    )
