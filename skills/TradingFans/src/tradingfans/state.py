"""
state.py — Module-level shared state singleton.

The engine writes to STATE; the dashboard server reads from it.
No locks needed — single asyncio process, no true concurrency.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class SpotQuote:
    price: float
    fresh: bool
    change_1m_pct: float   # % change over last 60 seconds
    change_5m_pct: float   # % change over last 300 seconds


@dataclass
class ActiveMarket:
    market_id: str
    question: str
    symbol: str            # BTC | ETH
    tte: float             # seconds to expiry
    implied_yes: float
    spread: float
    depth_ok: bool


@dataclass
class SignalRecord:
    ts: str                # HH:MM:SS
    signal: str            # BUY_YES | BUY_NO | NO_TRADE
    edge: float
    p_model: float
    implied_yes: float
    m1: float
    m5: float
    vol1: float
    size_usdc: float
    order_id: str | None
    question: str
    symbol: str


class AgentState:
    def __init__(self) -> None:
        self.started_at: float = time.time()
        self.dry_run: bool = True
        self.max_size: float = 10.0
        self.symbol_filter: str = "BOTH"

        # Live spot quotes
        self.btc: SpotQuote | None = None
        self.eth: SpotQuote | None = None

        # Active markets currently in the scan window
        self.active_markets: list[ActiveMarket] = []

        # Signal history
        self.recent_signals: deque[SignalRecord] = deque(maxlen=200)
        self.trade_count: int = 0
        self.scan_count: int = 0

        # Raw log lines for the live log panel
        self.log_lines: deque[str] = deque(maxlen=600)

    def uptime_str(self) -> str:
        secs = int(time.time() - self.started_at)
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def to_dict(self) -> dict:
        def quote(q: SpotQuote | None) -> dict | None:
            if q is None:
                return None
            return {
                "price": q.price,
                "fresh": q.fresh,
                "change_1m_pct": round(q.change_1m_pct * 100, 3),
                "change_5m_pct": round(q.change_5m_pct * 100, 3),
            }

        return {
            "uptime": self.uptime_str(),
            "dry_run": self.dry_run,
            "max_size": self.max_size,
            "symbol_filter": self.symbol_filter,
            "btc": quote(self.btc),
            "eth": quote(self.eth),
            "active_markets": [
                {
                    "market_id": m.market_id[:16],
                    "question": m.question,
                    "symbol": m.symbol,
                    "tte": round(m.tte, 1),
                    "implied_yes": round(m.implied_yes, 4),
                    "spread": round(m.spread, 4),
                    "depth_ok": m.depth_ok,
                }
                for m in self.active_markets
            ],
            "recent_signals": [
                {
                    "ts": s.ts,
                    "signal": s.signal,
                    "edge": round(s.edge, 4),
                    "p_model": round(s.p_model, 4),
                    "implied_yes": round(s.implied_yes, 4),
                    "m1": round(s.m1, 5),
                    "m5": round(s.m5, 5),
                    "vol1": round(s.vol1, 5),
                    "size_usdc": s.size_usdc,
                    "order_id": s.order_id,
                    "question": s.question,
                    "symbol": s.symbol,
                }
                for s in self.recent_signals
            ],
            "trade_count": self.trade_count,
            "scan_count": self.scan_count,
            "log_lines": list(self.log_lines)[-100:],
        }


# Module-level singleton — imported by engine and server
STATE = AgentState()
