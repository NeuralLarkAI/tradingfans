"""
state.py — Module-level shared state singleton.

The engine writes to STATE; the dashboard server reads from it.
No locks needed — single asyncio process, no true concurrency.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


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
    ts_epoch: float        # unix epoch seconds
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
    DRY_START_BALANCE: float = 10_000.0

    def __init__(self) -> None:
        self.started_at: float = time.time()
        self.dry_run: bool = True
        self.max_size: float = 10.0
        self.symbol_filter: str = "BOTH"
        self.poll_interval: float = 5.0
        self.min_edge: float = 0.02

        # Live spot quotes
        self.btc: SpotQuote | None = None
        self.eth: SpotQuote | None = None

        # Active markets currently in the scan window
        self.active_markets: list[ActiveMarket] = []

        # Signal history (newest first via appendleft)
        self.recent_signals: deque[SignalRecord] = deque(maxlen=200)
        self.trade_count: int = 0
        self.scan_count: int = 0

        # Dry-run portfolio tracking
        self.dry_deployed: float = 0.0   # running total USDC committed in dry-run

        # Mainnet wallet
        self.wallet_address: str = ""
        self.wallet_usdc: float | None = None
        self.wallet_matic: float | None = None

        # Raw log lines for the live log panel
        self.log_lines: deque[str] = deque(maxlen=600)

        # Autotuner (bounded self-optimization)
        self.tuner_enabled: bool = False
        self.tuner_status: str = "OFF"
        self.tuner_config_path: str = ""
        self.tuner_last_run: float | None = None
        self.tuner_allowlist: list[str] = []
        self.tuner_params: dict[str, float] = {}
        self.tuner_specs: list[dict] = []
        self.tuner_events: deque[dict] = deque(maxlen=250)

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

        all_sigs = list(self.recent_signals)

        # Dry-run trades: non-NO_TRADE signals with size > 0 in dry mode
        dry_trades = [
            s for s in all_sigs
            if s.signal != "NO_TRADE" and s.size_usdc > 0
            and (s.order_id == "dry-run" or (self.dry_run and s.order_id is None and s.size_usdc > 0))
        ]
        # Mainnet trades: real order IDs (not "dry-run", not None)
        live_trades = [
            s for s in all_sigs
            if s.order_id and s.order_id != "dry-run"
        ]

        exp_pnl = sum(abs(s.edge) * s.size_usdc for s in dry_trades)
        deployed = self.dry_deployed
        available = max(0.0, self.DRY_START_BALANCE - deployed)

        def trade_dict(s: SignalRecord, mode: str) -> dict:
            return {
                "ts": s.ts,
                "ts_epoch": s.ts_epoch,
                "symbol": s.symbol,
                "signal": s.signal,
                "size_usdc": round(s.size_usdc, 2),
                "price": round(s.implied_yes * 100, 1),       # as % for display
                "edge": round(s.edge * 100, 2),               # as % for display
                "exp_pnl": round(abs(s.edge) * s.size_usdc, 2),
                "question": s.question,
                "order_id": s.order_id or "",
                "mode": mode,
            }

        return {
            "uptime": self.uptime_str(),
            "dry_run": self.dry_run,
            "max_size": self.max_size,
            "symbol_filter": self.symbol_filter,
            "poll_interval": self.poll_interval,
            "min_edge": self.min_edge,
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
                    "ts_epoch": s.ts_epoch,
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
                for s in all_sigs
            ],
            "trade_count": self.trade_count,
            "scan_count": self.scan_count,
            "log_lines": list(self.log_lines)[-100:],
            # ── Dry-run portfolio ──────────────────────────────────
            "dry_balance": {
                "start":    self.DRY_START_BALANCE,
                "deployed": round(deployed, 2),
                "available": round(available, 2),
                "exp_pnl":  round(exp_pnl, 2),
                "trade_count": len(dry_trades),
            },
            "dry_trades":  [trade_dict(s, "DRY")  for s in dry_trades[:100]],
            "live_trades": [trade_dict(s, "LIVE") for s in live_trades[:100]],
            # ── Mainnet wallet ────────────────────────────────────
            "wallet": {
                "address": self.wallet_address,
                "usdc":    round(self.wallet_usdc, 2)  if self.wallet_usdc  is not None else None,
                "matic":   round(self.wallet_matic, 4) if self.wallet_matic is not None else None,
            },
            # ── Autotuner ───────────────────────────────────────────────
            "tuner": {
                "enabled": self.tuner_enabled,
                "status": self.tuner_status,
                "config_path": self.tuner_config_path,
                "last_run_epoch": self.tuner_last_run,
                "allowlist": list(self.tuner_allowlist),
                "params": dict(self.tuner_params),
                "specs": list(self.tuner_specs),
                "events": list(self.tuner_events),
            },
        }


# Module-level singleton — imported by engine and server
STATE = AgentState()
