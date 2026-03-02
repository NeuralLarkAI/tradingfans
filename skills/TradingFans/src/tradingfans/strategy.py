"""
strategy.py — Small, bounded set of strategy presets + selector.

This is intentionally conservative: no arbitrary self-modifying code. The agent
can only switch between predefined presets based on realized outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyPreset:
    name: str
    w_m1: float
    w_m5: float
    w_vol: float
    min_edge: float


PRESETS: dict[str, StrategyPreset] = {
    # Default: short-horizon momentum
    "momentum": StrategyPreset(name="momentum", w_m1=20.0, w_m5=8.0, w_vol=-4.0, min_edge=0.02),
    # Alternate: mean reversion (negate momentum weights)
    "mean_reversion": StrategyPreset(name="mean_reversion", w_m1=-18.0, w_m5=-6.0, w_vol=-2.0, min_edge=0.02),
    # More selective momentum (fewer trades)
    "conservative": StrategyPreset(name="conservative", w_m1=18.0, w_m5=6.0, w_vol=-6.0, min_edge=0.03),
}


def pick_strategy(
    *,
    current: str,
    last_n: list[dict],
    min_trades: int = 12,
) -> str:
    """
    Decide whether to switch presets based on realized recent performance.
    Uses a simple win-rate + pnl heuristic with hysteresis to avoid flapping.
    """
    if len(last_n) < min_trades:
        return current

    wins = sum(1 for t in last_n if float(t.get("pnl_usdc", 0.0)) > 0)
    pnl = sum(float(t.get("pnl_usdc", 0.0)) for t in last_n)
    win_rate = wins / max(1, len(last_n))

    # If clearly losing, try the opposite regime.
    if current == "momentum" and (win_rate < 0.45 and pnl < 0):
        return "mean_reversion"
    if current == "mean_reversion" and (win_rate < 0.45 and pnl < 0):
        return "momentum"

    # If doing well, keep it.
    return current

