"""
engine.py — Main asyncio trading engine.

Now includes:
  - Dashboard server (http://localhost:7331) started alongside the trade loop
  - STATE singleton populated in real-time (spot quotes, markets, signals, logs)
  - Custom log handler that routes all log lines to STATE.log_lines for the UI
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import os
import signal
import sys
from pathlib import Path
import time
from collections import deque
import re

from .clob import ClobClient, check_depth
from .decision import DecisionResult, compute as decision_compute
from .gamma import GammaCache, Market
from .llm_advisor import LLMAdvisor, TradeEvent
from .risk import RiskCheck, evaluate as risk_evaluate
from .server import start_server
from .spot import SpotFeed
from .state import STATE, ActiveMarket, SignalRecord, SpotQuote
from .tuner import autotune_loop, init_from_config
from .telegram_remote import telegram_loop
from .performance import OpenTrade, record_open_trade, resolve_due_trades
from .strategy import PRESETS, pick_strategy
from .llm_trader import decide as llm_decide, enabled as llm_enabled
from .agents import (
    AgentConfig,
    MAX_AGENTS,
    assign_agent_id,
    default_pool,
    maybe_enable_multi_agent,
    mutate,
    to_public_dict,
)

# ── Custom log handler → STATE.log_lines ──────────────────────

class _StateHandler(logging.Handler):
    """Captures every log record into STATE so the dashboard can display it."""
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            STATE.log_lines.append(msg)
        except Exception:
            pass


def _setup_logging() -> None:
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-18s — %(message)s",
        datefmt="%H:%M:%S",
    )
    # Console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    # State handler (feeds the dashboard)
    sh = _StateHandler()
    sh.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(ch)
    root.addHandler(sh)


log = logging.getLogger("engine")

# ── Config ────────────────────────────────────────────────────
def _maybe_load_dotenv() -> None:
    """
    Load a nearby `.env` file (walking up a few parent dirs).
    Only sets variables that are not already present in the process env.
    """
    if os.environ.get("TRADINGFANS_NO_DOTENV", "0") == "1":
        return
    try:
        cwd = Path.cwd()
        for p in [cwd, *list(cwd.parents)[:5]]:
            env_path = p / ".env"
            if not env_path.exists():
                continue
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().lstrip("\ufeff")
                v = v.strip()
                if not k or k in os.environ:
                    continue
                if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                    v = v[1:-1]
                os.environ[k] = v
            log.info("Loaded .env from %s", str(env_path))
            break
    except Exception:
        pass

MAX_SIZE_USDC  = float(os.environ.get("POLY_MAX_SIZE", "10"))
POLL_INTERVAL  = float(os.environ.get("POLY_POLL_INTERVAL", "5"))
SYMBOL_FILTER  = os.environ.get("POLY_SYMBOL", "both").upper()

_open_positions: set[str] = set()
_decision_log: deque[dict] = deque(maxlen=200)


# ── Helpers ───────────────────────────────────────────────────

_SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("bitcoin",),
    "ETH": ("ethereum",),
    "SOL": ("solana",),
    "XRP": ("ripple",),
    "ADA": ("cardano",),
    "DOGE": ("dogecoin",),
    "AVAX": ("avalanche",),
    "LINK": ("chainlink",),
    "MATIC": ("polygon",),
    "DOT": ("polkadot",),
    "LTC": ("litecoin",),
    "BCH": ("bitcoin cash",),
    "ATOM": ("cosmos",),
    "UNI": ("uniswap",),
    "AAVE": ("aave",),
    "ETC": ("ethereum classic",),
    "XLM": ("stellar",),
    "ALGO": ("algorand",),
    "NEAR": ("near", "near protocol"),
    "FIL": ("filecoin",),
}


def _detect_symbol(market: Market, *, supported: set[str]) -> str | None:
    text = (market.question + " " + market.slug).lower()

    # Prefer tickers present as whole words.
    for sym in sorted(supported, key=len, reverse=True):
        if re.search(rf"\\b{re.escape(sym.lower())}\\b", text):
            return sym

    # Fall back to name aliases.
    for sym, aliases in _SYMBOL_ALIASES.items():
        if sym not in supported:
            continue
        for a in aliases:
            if a in text:
                return sym
    return None


def _size_usdc(edge: float, max_size: float, *, edge_full_scale: float | None = None) -> float:
    scale_in = float(edge_full_scale) if edge_full_scale is not None else float(getattr(STATE, "edge_full_scale", 0.05))
    scale = max(0.001, scale_in)
    # Convex sizing: small edges still get meaningful size, while large edges saturate at max_size.
    ratio = max(0.0, abs(edge) / scale)
    fraction = min(ratio ** 0.5, 1.0)
    return round(max(fraction * max_size, 0.0), 2)


def _get_float_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except Exception:
        return float(default)


def _utc_now_iso() -> str:
    return datetime.datetime.now(tz=datetime.timezone.utc).isoformat()


def _hms() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def _update_spot_state(spot: SpotFeed) -> None:
    """Push latest spot data into STATE for the dashboard."""
    for sym in ("BTC", "ETH"):
        window = spot.window(sym)
        fresh = spot.is_fresh(sym)
        if not window:
            continue

        price_now = window[-1][1]
        now_ts = window[-1][0]

        # 1m momentum
        cutoff_1m = now_ts - 60
        old_1m = next((px for ts, px in reversed(window) if ts <= cutoff_1m), None)
        change_1m = (price_now - old_1m) / old_1m if old_1m else 0.0

        # 5m momentum
        cutoff_5m = now_ts - 300
        old_5m = next((px for ts, px in reversed(window) if ts <= cutoff_5m), None)
        change_5m = (price_now - old_5m) / old_5m if old_5m else 0.0

        q = SpotQuote(price=price_now, fresh=fresh, change_1m_pct=change_1m, change_5m_pct=change_5m)
        if sym == "BTC":
            STATE.btc = q
        else:
            STATE.eth = q

    # Downsampled spot series for charting (symbols we can actually quote).
    try:
        for sym in spot.supported_symbols():
            w = spot.window(sym)
            if not w:
                continue
            # Downsample to <= ~180 points to keep /api/state light.
            step = max(1, int(len(w) / 180))
            STATE.spot_series[sym] = w[::step]
    except Exception:
        pass


async def _wait_for_spot_history(spot: SpotFeed, *, symbols: list[str], min_age_sec: float = 60.0, timeout_sec: float = 90.0) -> None:
    """
    Wait until the spot window contains at least min_age_sec of history for each symbol.
    This prevents the decision model from seeing m1/m5 as 0.0 due to insufficient history.
    """
    start = time.monotonic()
    log.info("Warming up spot history (need %.0fs)…", min_age_sec)
    while True:
        ready = True
        for sym in symbols:
            w = spot.window(sym)
            if not w:
                ready = False
                break
            now_ts = w[-1][0]
            if not any(ts <= (now_ts - min_age_sec) for ts, _ in w):
                ready = False
                break
        if ready:
            log.info("Spot warm-up complete.")
            return
        if (time.monotonic() - start) >= timeout_sec:
            log.warning("Spot warm-up timed out after %.0fs — continuing anyway.", timeout_sec)
            return
        await asyncio.sleep(2.0)


# ── Per-market evaluation ─────────────────────────────────────

async def _evaluate_market(
    market: Market,
    spot: SpotFeed,
    clob: ClobClient,
    agent: AgentConfig,
    symbol_filter: str,
    loop: asyncio.AbstractEventLoop,
) -> tuple[Market, DecisionResult, float, str | None] | None:

    if market.market_id in _open_positions:
        return None

    supported = set(spot.supported_symbols())
    symbol = _detect_symbol(market, supported=supported)
    if symbol is None:
        return None
    if symbol_filter not in ("BOTH", "ALL", symbol):
        return None

    spot_fresh = spot.is_fresh(symbol)
    window = spot.window(symbol)
    if not window:
        return None
    # Ensure we can resolve outcomes for markets expiring very soon:
    # We need a spot tick at (end_epoch - 300). If time_to_expiry < 300s, that timestamp is in the past.
    # Require enough history to cover (300 - tte) seconds; otherwise the trade may become UNRESOLVED.
    age = float(window[-1][0] - window[0][0])
    need_hist = max(0.0, 300.0 - float(market.time_to_expiry)) + 5.0
    if age < need_hist:
        log.info("RISK FAIL | %s | spot_history<%.0fs", market.question[:40], need_hist)
        return None

    book = await loop.run_in_executor(None, clob.get_book, market.yes_token_id)

    # Many 5m markets have pathological CLOB books (best_bid~0.01/best_ask~0.99 => mid~0.50).
    # In DRY RUN we allow a Gamma-derived implied price so the UI and model aren't stuck at 50%.
    implied_yes: float | None = None
    if book and book.mid is not None:
        implied_yes = float(book.mid)
        if STATE.dry_run and book.spread is not None and float(book.spread) >= 0.50:
            implied_yes = None

    if implied_yes is None:
        implied_yes = float(market.gamma_mid) if market.gamma_mid is not None else None

    # In live mode, if we still don't have a price/book, skip.
    if implied_yes is None or (not STATE.dry_run and book is None):
        return None

    rc: RiskCheck = risk_evaluate(
        time_to_expiry=market.time_to_expiry,
        spot_fresh=spot_fresh,
        book=book,
        implied_yes=implied_yes,
        max_time_to_expiry=STATE.max_time_to_expiry,
    )
    if not rc.ok:
        log.info("RISK FAIL | %s | %s", market.question[:40], " | ".join(rc.reasons))
        return None

    if agent.brain == "llm" and llm_enabled(dry_run=STATE.dry_run):
        # Use the quant feature extractor to compute m1/m5/vol1, then let the LLM estimate fair p_model.
        feats = decision_compute(window=window, implied_yes=implied_yes, min_edge=0.0, w_m1=0.0, w_m5=0.0, w_vol=0.0)
        llm_d = await llm_decide(
            market_id=market.market_id,
            question=market.question,
            implied_yes=float(implied_yes),
            tte_sec=float(market.time_to_expiry),
            m1=float(feats.m1),
            m5=float(feats.m5),
            vol1=float(feats.vol1),
        )
        p_model = float(llm_d.p_model)
        edge = p_model - float(implied_yes)
        if abs(edge) < float(agent.min_edge):
            signal = "NO_TRADE"
        else:
            signal = "BUY_YES" if edge > 0 else "BUY_NO"
        decision = DecisionResult(
            p_model=p_model,
            edge=edge,
            m1=float(feats.m1),
            m5=float(feats.m5),
            vol1=float(feats.vol1),
            signal=signal,
            confidence=abs(edge),
            reason=str(llm_d.reason),
        )
    else:
        decision = decision_compute(
            window=window,
            implied_yes=implied_yes,
            min_edge=float(agent.min_edge),
            w_m1=float(agent.w_m1),
            w_m5=float(agent.w_m5),
            w_vol=float(agent.w_vol),
        )

    log.info(
        "SIGNAL %-8s | tte=%3.0fs | impl=%.3f | model=%.3f | edge=%+.4f | %s",
        decision.signal, market.time_to_expiry, implied_yes,
        decision.p_model, decision.edge, market.question[:35],
    )

    # Always record the signal (even NO_TRADE) for the dashboard
    STATE.recent_signals.appendleft(
        SignalRecord(
            ts=_hms(),
            ts_epoch=datetime.datetime.now(tz=datetime.timezone.utc).timestamp(),
            expires_epoch=market.end_time.timestamp(),
            signal=decision.signal,
            edge=decision.edge,
            p_model=decision.p_model,
            implied_yes=implied_yes,
            price_paid=implied_yes,
            m1=decision.m1,
            m5=decision.m5,
            vol1=decision.vol1,
            size_usdc=0.0,
            order_id=None,
            question=market.question,
            symbol=symbol,
        )
    )

    if decision.signal == "NO_TRADE":
        return None

    size = max(
        _size_usdc(
            decision.edge,
            float(agent.max_size_usdc),
            edge_full_scale=float(agent.edge_full_scale),
        ),
        float(agent.min_order_size_usdc),
    )
    if size < float(agent.min_order_size_usdc):
        log.debug(
            "Order size %.2f USDC < minimum %.2f — skipping.",
            size,
            float(agent.min_order_size_usdc),
        )
        return None

    # Live-mode wallet-based caps to prevent spending the whole wallet quickly.
    # These are soft constraints on top of per-agent max_size_usdc.
    if not STATE.dry_run:
        # Use Polymarket collateral balance when available; fall back to on-chain wallet USDC.
        wallet_usdc = STATE.poly_collateral_usdc if STATE.poly_collateral_usdc is not None else STATE.wallet_usdc
        reserve = _get_float_env("POLY_LIVE_WALLET_RESERVE_USDC", 20.0)
        trade_frac = _get_float_env("POLY_LIVE_TRADE_FRACTION", 0.05)      # max % of spendable per trade
        total_frac = _get_float_env("POLY_LIVE_TOTAL_FRACTION", 0.25)      # max % of spendable across open exposure
        if wallet_usdc is not None:
            spendable = max(0.0, float(wallet_usdc) - float(reserve))
            max_trade = max(0.0, spendable * max(0.0, min(1.0, trade_frac)))
            max_total = max(0.0, spendable * max(0.0, min(1.0, total_frac)))
            if spendable <= 0.0:
                log.info("RISK FAIL | %s | wallet_spendable<=0", market.question[:40])
                return None
            size = min(size, max_trade if max_trade > 0 else size, spendable)
            if size < float(agent.min_order_size_usdc):
                log.info("RISK FAIL | %s | wallet_cap<size", market.question[:40])
                return None
            if (float(getattr(STATE, "live_deployed", 0.0)) + size) > (max_total + 1e-6):
                log.info("RISK FAIL | %s | live_exposure_cap", market.question[:40])
                return None

    # Execution price:
    # - DRY RUN: simulate fills at the implied mid (AMM-like) to avoid pathological CLOB spreads
    #            in 5m markets (often best_bid≈0.01 / best_ask≈0.99).
    # - LIVE:    use best_ask on the relevant token (and rely on risk spread/depth checks).
    if decision.signal == "BUY_YES":
        token_id = market.yes_token_id
        price = float(implied_yes) if STATE.dry_run else float(book.best_ask if book.best_ask else implied_yes)
    else:
        token_id = market.no_token_id
        implied_no = 1.0 - implied_yes
        if STATE.dry_run:
            price = float(implied_no)
        else:
            book_no = await loop.run_in_executor(None, clob.get_book, token_id)
            price = float(book_no.best_ask if (book_no and book_no.best_ask) else implied_no)
        price = max(0.01, min(float(price), 0.99))

    log.info(
        "🔔 TRADE | %-8s | size=%.2f USDC | price=%.4f | edge=%+.4f | %s",
        decision.signal, size, price, decision.edge, market.question[:55],
    )

    # Update the last signal record with the computed execution price (even if order fails).
    if STATE.recent_signals:
        sr0 = STATE.recent_signals[0]
        STATE.recent_signals[0] = SignalRecord(
            ts=sr0.ts, signal=sr0.signal, edge=sr0.edge, p_model=sr0.p_model,
            implied_yes=sr0.implied_yes, price_paid=price, m1=sr0.m1, m5=sr0.m5, vol1=sr0.vol1,
            size_usdc=sr0.size_usdc, order_id=sr0.order_id, question=sr0.question, symbol=sr0.symbol,
            ts_epoch=sr0.ts_epoch, expires_epoch=sr0.expires_epoch,
        )

    order_id = await loop.run_in_executor(
        None, clob.place_order, token_id, decision.signal, size, price
    )

    if order_id:
        _open_positions.add(market.market_id)
        STATE.trade_count += 1
        try:
            perf = STATE.agent_perf.setdefault(agent.agent_id, {"trades": 0, "resolved": 0, "realized_pnl": 0.0})
            perf["trades"] = int(perf.get("trades", 0)) + 1
        except Exception:
            pass

        if STATE.dry_run:
            STATE.dry_deployed += size   # track virtual capital committed
            mode = "DRY"
        else:
            STATE.live_deployed = float(getattr(STATE, "live_deployed", 0.0)) + float(size)
            mode = "LIVE"

        # Track trade for later expiry release / (dry-run) realized PnL.
        record_open_trade(OpenTrade(
            market_id=market.market_id,
            question=market.question,
            symbol=symbol,
            side=decision.signal,
            size_usdc=size,
            price_paid=price,
            entry_epoch=time.time(),
            end_epoch=market.end_time.timestamp(),
            agent_id=agent.agent_id,
            mode=mode,
        ))
        # Update the last signal record with the real order_id
        if STATE.recent_signals:
            sr = STATE.recent_signals[0]
            STATE.recent_signals[0] = SignalRecord(
                ts=sr.ts, signal=sr.signal, edge=sr.edge, p_model=sr.p_model,
                implied_yes=sr.implied_yes, price_paid=sr.price_paid, m1=sr.m1, m5=sr.m5, vol1=sr.vol1,
                size_usdc=size, order_id=order_id, question=sr.question, symbol=sr.symbol,
                ts_epoch=sr.ts_epoch,
                expires_epoch=sr.expires_epoch,
            )

    return market, decision, size, order_id


# ── Wallet balance polling ────────────────────────────────────

async def _fetch_wallet_balance() -> tuple[float | None, float | None]:
    """Fetch USDC and MATIC balance from Polygon via public RPC."""
    import aiohttp as _aiohttp
    address = STATE.wallet_address
    if not address:
        return None, None

    # Public RPCs can be flaky or rate-limited. Try a small fallback list.
    rpc_env = os.environ.get("POLY_POLYGON_RPC", "").strip()
    RPCS = [rpc_env] if rpc_env else []
    RPCS += [
        "https://polygon-bor.publicnode.com",
        "https://polygon-rpc.com",
    ]
    # Polygon has both bridged USDC.e and native USDC (Circle). Users may deposit either.
    USDC_E_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"       # USDC.e (bridged)
    USDC_NATIVE_CONTRACT = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # USDC (native)

    try:
        addr_raw = (address[2:] if address.startswith("0x") else address).lower()
        calldata = "0x70a08231" + addr_raw.zfill(64)   # balanceOf(address)

        timeout = _aiohttp.ClientTimeout(total=8)
        async with _aiohttp.ClientSession(timeout=timeout) as sess:
            async def _post(rpc: str, payload: dict) -> dict:
                async with sess.post(rpc, json=payload) as r:
                    return await r.json()

            async def _erc20_balance(rpc: str, contract: str, req_id: int) -> float:
                j = await _post(rpc, {
                    "jsonrpc": "2.0",
                    "method": "eth_call",
                    "params": [{"to": contract, "data": calldata}, "latest"],
                    "id": req_id,
                })
                hex_u = j.get("result", "0x0") if isinstance(j, dict) else "0x0"
                return int(hex_u, 16) / 1_000_000  # 6 decimals

            async def _matic_balance(rpc: str) -> float:
                j = await _post(rpc, {
                    "jsonrpc": "2.0",
                    "method": "eth_getBalance",
                    "params": [address, "latest"],
                    "id": 10,
                })
                hex_m = j.get("result", "0x0") if isinstance(j, dict) else "0x0"
                return int(hex_m, 16) / 1e18

            last_exc: Exception | None = None
            for rpc in [r for r in RPCS if r]:
                try:
                    usdc_e = await _erc20_balance(rpc, USDC_E_CONTRACT, 1)
                    usdc_native = await _erc20_balance(rpc, USDC_NATIVE_CONTRACT, 2)
                    matic = await _matic_balance(rpc)
                    usdc_total = float(usdc_e) + float(usdc_native)
                    STATE.wallet_usdc_e = float(usdc_e)
                    STATE.wallet_usdc_native = float(usdc_native)
                    return usdc_total, matic
                except Exception as exc:
                    last_exc = exc
                    continue
            if last_exc:
                raise last_exc
            return None, None
    except Exception as exc:
        log.debug("Wallet fetch failed: %s", exc)
        return None, None


async def wallet_poll_loop(clob: ClobClient) -> None:
    """Poll Polygon wallet + Polymarket collateral balance every 60 seconds."""
    while True:
        usdc, matic = await _fetch_wallet_balance()
        if usdc is not None:
            STATE.wallet_usdc = usdc
        if matic is not None:
            STATE.wallet_matic = matic

        try:
            # Level-2 auth call to CLOB; this is what the Polymarket UI calls "balance".
            col = clob.get_collateral_balance_usdc()
            if col is not None:
                STATE.poly_collateral_usdc = float(col)
        except Exception:
            pass

        if usdc is not None or matic is not None:
            log.debug(
                "Wallet: USDC=%.2f (native=%.2f usdc.e=%.2f) collateral=%.2f MATIC=%.4f",
                float(STATE.wallet_usdc or 0.0),
                float(getattr(STATE, "wallet_usdc_native", 0.0) or 0.0),
                float(getattr(STATE, "wallet_usdc_e", 0.0) or 0.0),
                float(getattr(STATE, "poly_collateral_usdc", 0.0) or 0.0),
                float(STATE.wallet_matic or 0.0),
            )
        await asyncio.sleep(60)


async def evolve_agents_loop() -> None:
    """
    Bounded "evolution" loop for DRY RUN multi-agent mode.

    Spawns a mutation from the current best agent (by realized pnl) and replaces the
    current worst agent. Max MAX_AGENTS total. No code changes, only parameters.
    """
    last_evolve: float = 0.0
    while True:
        try:
            await asyncio.sleep(20.0)
            if not STATE.dry_run:
                continue
            if not STATE.multi_agent_enabled:
                continue
            if not getattr(STATE, "agent_evolution_enabled", True):
                continue
            if getattr(STATE, "primary_agent_id", ""):
                continue

            pool: list[AgentConfig] = getattr(STATE, "_agent_pool", [])  # type: ignore[attr-defined]
            if not pool or len(pool) < 2:
                continue

            perf = STATE.agent_perf or {}

            def _metrics(aid: str) -> tuple[float, int]:
                try:
                    p = perf.get(aid) or {}
                    return float(p.get("realized_pnl", 0.0)), int(p.get("resolved", 0))
                except Exception:
                    return 0.0, 0

            total_resolved = sum(_metrics(a.agent_id)[1] for a in pool)
            if total_resolved < 6:
                continue

            now = time.time()
            if (now - last_evolve) < 120.0:
                continue

            ranked = sorted(pool, key=lambda a: (_metrics(a.agent_id)[0], _metrics(a.agent_id)[1]), reverse=True)
            best = ranked[0]
            worst = ranked[-1]
            if best.agent_id == worst.agent_id:
                continue

            child = mutate(best)
            pool2 = [a for a in pool if a.agent_id != worst.agent_id]
            pool2.append(child)
            pool2 = pool2[:MAX_AGENTS]

            STATE._agent_pool = pool2  # type: ignore[attr-defined]
            STATE.agents = [to_public_dict(a) for a in pool2]
            STATE.agent_perf.setdefault(child.agent_id, {"trades": 0, "resolved": 0, "realized_pnl": 0.0})

            try:
                STATE.tuner_events.appendleft({
                    "ts_epoch": now,
                    "key": "agents.evolve",
                    "old": worst.agent_id,
                    "new": child.agent_id,
                    "reason": f"replace_worst_spawn_from_{best.agent_id}",
                    "metrics": {
                        "best_pnl": _metrics(best.agent_id)[0],
                        "worst_pnl": _metrics(worst.agent_id)[0],
                        "total_resolved": total_resolved,
                    },
                })
            except Exception:
                pass

            log.info("AGENTS evolve: spawned %s from %s (replaced %s)", child.agent_id, best.agent_id, worst.agent_id)
            last_evolve = now
        except asyncio.CancelledError:
            return
        except Exception:
            continue


# ── Main trading loop ─────────────────────────────────────────

async def trading_loop(
    gamma: GammaCache,
    spot: SpotFeed,
    clob: ClobClient,
    llm: LLMAdvisor,
    symbol_filter: str,
) -> None:
    loop = asyncio.get_running_loop()
    log.info(
        "Trading loop live | poll=%.1fs | symbol=%s | multi_agent=%s",
        STATE.poll_interval, symbol_filter, "ON" if STATE.multi_agent_enabled else "OFF",
    )

    while True:
        try:
            # Update spot state for dashboard
            _update_spot_state(spot)

            # Resolve any finished trades and update realized performance
            resolve_due_trades(spot)

            # Strategy selection (bounded presets based on realized outcomes)
            last = list(STATE.resolved_trades)[:20]
            current = str(STATE.strategy.get("name", "momentum"))
            nxt = pick_strategy(current=current, last_n=last)
            if nxt != current and nxt in PRESETS:
                p = PRESETS[nxt]
                STATE.strategy = {"name": p.name, "w_m1": p.w_m1, "w_m5": p.w_m5, "w_vol": p.w_vol}
                # Strategy change is a meaningful event; surface it via tuner events too
                try:
                    STATE.tuner_events.appendleft({
                        "ts_epoch": time.time(),
                        "key": "strategy",
                        "old": current,
                        "new": nxt,
                        "reason": "performance_switch",
                        "metrics": {"resolved": len(last)},
                    })
                except Exception:
                    pass

            markets = await gamma.active_markets()
            STATE.scan_count += 1

            # Build active-market records for dashboard
            active: list[ActiveMarket] = []
            max_tte = float(STATE.max_time_to_expiry or 900.0)
            for m in markets:
                sym = _detect_symbol(m, supported=set(spot.supported_symbols()))
                if sym is None:
                    continue
                if not (0.0 < m.time_to_expiry < max_tte):
                    continue
                book = await loop.run_in_executor(None, clob.get_book, m.yes_token_id)

                implied = None
                spread = 0.0
                depth_ok = False
                if book:
                    implied = float(book.mid)
                    spread = float(book.spread)
                    depth_ok = check_depth(book, book.mid)
                    if STATE.dry_run and spread >= 0.50 and m.gamma_mid is not None:
                        implied = float(m.gamma_mid)
                elif STATE.dry_run and m.gamma_mid is not None:
                    implied = float(m.gamma_mid)
                    spread = 1.0
                    depth_ok = True

                if implied is not None:
                    active.append(ActiveMarket(
                        market_id=m.market_id,
                        question=m.question,
                        symbol=sym,
                        tte=m.time_to_expiry,
                        implied_yes=float(implied),
                        spread=float(spread),
                        depth_ok=bool(depth_ok),
                    ))
            STATE.active_markets = active

            if STATE.paused:
                log.info("PAUSED — skipping evaluation loop.")
            elif not markets:
                log.debug("No active 5m crypto markets — waiting.")
            else:
                # Only evaluate markets in the configured time window.
                max_tte = float(STATE.max_time_to_expiry or 900.0)
                markets = [m for m in markets if 0.0 < m.time_to_expiry < max_tte]
                pool: list[AgentConfig] = getattr(STATE, "_agent_pool", [])  # type: ignore[attr-defined]
                if not pool:
                    pool = default_pool()
                    # seed bounded variants for "evolution"
                    try:
                        pool.append(mutate(pool[0]))
                        pool.append(mutate(pool[1]))
                    except Exception:
                        pass
                    pool = pool[:MAX_AGENTS]
                    STATE._agent_pool = pool  # type: ignore[attr-defined]
                    STATE.agents = [to_public_dict(a) for a in pool]
                    for a in pool:
                        STATE.agent_perf.setdefault(a.agent_id, {"trades": 0, "resolved": 0, "realized_pnl": 0.0})

                agent_ids = [a.agent_id for a in pool]
                tasks = []
                for m in markets:
                    primary = str(getattr(STATE, "primary_agent_id", "") or "")
                    if primary and any(a.agent_id == primary for a in pool):
                        aid = primary
                    elif STATE.multi_agent_enabled:
                        aid = assign_agent_id(m.market_id, agent_ids)
                    else:
                        aid = agent_ids[0] if agent_ids else ""
                    agent = next((a for a in pool if a.agent_id == aid), pool[0])
                    tasks.append(_evaluate_market(m, spot, clob, agent, symbol_filter, loop))
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for market, result in zip(markets, results):
                    if isinstance(result, Exception):
                        log.warning("Market eval error [%s…]: %s", market.market_id[:12], result)
                    elif result is not None:
                        mkt, decision, size, order_id = result
                        _decision_log.append({
                            "ts": _utc_now_iso(),
                            "market_id": mkt.market_id[:16],
                            "signal": decision.signal,
                            "edge": round(decision.edge, 4),
                            "order_id": order_id,
                        })
                        llm.queue_trade(TradeEvent(
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
                        ))

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Trading loop error: %s", exc, exc_info=True)

        await asyncio.sleep(max(0.25, STATE.poll_interval))


# ── Entry point ───────────────────────────────────────────────

async def main() -> None:
    _setup_logging()
    _maybe_load_dotenv()

    parser = argparse.ArgumentParser(description="TradingFans — Polymarket 5m crypto agent")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-size", type=float, default=None, metavar="USDC")
    parser.add_argument("--symbol", choices=["BTC", "ETH", "both", "all"], default=None)
    args = parser.parse_args()

    if args.dry_run:
        os.environ["POLY_DRY_RUN"] = "1"
    if args.max_size is not None:
        os.environ["POLY_MAX_SIZE"] = str(args.max_size)
    if args.symbol is not None:
        os.environ["POLY_SYMBOL"] = args.symbol

    max_size = float(os.environ.get("POLY_MAX_SIZE", str(MAX_SIZE_USDC)))
    poll_interval = float(os.environ.get("POLY_POLL_INTERVAL", str(POLL_INTERVAL)))
    sym_filter = os.environ.get("POLY_SYMBOL", "all").upper()
    if sym_filter == "BOTH":
        sym_filter = "ALL"
    dry = os.environ.get("POLY_DRY_RUN", "0") == "1"

    # Populate STATE config fields
    STATE.dry_run = dry
    STATE.max_size = max_size
    STATE.symbol_filter = sym_filter
    STATE.poll_interval = poll_interval

    log.info("=" * 60)
    log.info("TradingFans v1.0.0  |  Polymarket 5m Crypto Agent")
    log.info("  DRY RUN : %s", dry)
    log.info("  Max size: %.2f USDC", max_size)
    log.info("  Symbol  : %s", sym_filter)
    log.info("  Poll    : %.1fs", poll_interval)
    log.info("  MinEdge : %.3f", STATE.min_edge)
    log.info("  MinSize : %.2f USDC", STATE.min_order_size)
    log.info("  MaxTTE  : %.0fs", STATE.max_time_to_expiry)
    log.info("=" * 60)

    # Load wallet address into STATE for the dashboard
    STATE.wallet_address = os.environ.get("POLY_FUNDER", "").strip()

    # Load tuner config into STATE (and possibly enable tuning)
    init_from_config(dry_run=dry)

    # Initialize strategy preset
    preset_name = os.environ.get("POLY_STRATEGY", "").strip() or str(STATE.strategy.get("name", "momentum"))
    if preset_name in PRESETS:
        p = PRESETS[preset_name]
        STATE.strategy = {"name": p.name, "w_m1": p.w_m1, "w_m5": p.w_m5, "w_vol": p.w_vol}

    # Multi-agent pool (logical agents; DRY RUN default ON).
    STATE.multi_agent_enabled = maybe_enable_multi_agent(dry)
    pool = default_pool()
    try:
        pool.append(mutate(pool[0]))
        pool.append(mutate(pool[1]))
    except Exception:
        pass
    pool = pool[:MAX_AGENTS]
    STATE._agent_pool = pool  # type: ignore[attr-defined]
    STATE.agents = [to_public_dict(a) for a in pool]
    for a in pool:
        STATE.agent_perf.setdefault(a.agent_id, {"trades": 0, "resolved": 0, "realized_pnl": 0.0})

    # Disable autotuner in multi-agent mode to avoid hidden cross-agent coupling.
    if STATE.multi_agent_enabled:
        STATE.tuner_enabled = False
        STATE.tuner_status = "OFF"

    gamma = GammaCache()
    spot  = SpotFeed()
    clob  = ClobClient()
    llm   = LLMAdvisor()

    # Graceful shutdown
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal(sig: signal.Signals) -> None:
        log.info("Received %s — shutting down...", sig.name)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, OSError):
            signal.signal(sig, lambda s, f: stop_event.set())

    async def _watch_stop_flag() -> None:
        while not stop_event.is_set():
            if getattr(STATE, "stop_requested", False):
                log.info("STOP requested via dashboard — shutting down...")
                stop_event.set()
                return
            await asyncio.sleep(0.25)

    # Start subsystems
    ui_runner = await start_server()
    await spot.start()
    await llm.start()

    log.info("Spot feed started — accumulating 5m history before trading...")
    await asyncio.sleep(3.0)

    trade_task = asyncio.create_task(
        trading_loop(gamma, spot, clob, llm, sym_filter),
        name="trading-loop",
    )
    wallet_task = asyncio.create_task(
        wallet_poll_loop(clob),
        name="wallet-poll",
    )
    tuner_task = asyncio.create_task(
        autotune_loop(),
        name="autotune",
    )
    remote_task = asyncio.create_task(
        telegram_loop(),
        name="telegram-remote",
    )
    evolve_task = asyncio.create_task(
        evolve_agents_loop(),
        name="agent-evolve",
    )
    stop_watch_task = asyncio.create_task(
        _watch_stop_flag(),
        name="stop-watch",
    )

    await stop_event.wait()
    log.info("Shutting down...")

    for task in (trade_task, wallet_task, tuner_task, remote_task, evolve_task, stop_watch_task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await spot.stop()
    await llm.stop()
    await ui_runner.cleanup()

    if _decision_log:
        log.info("Session: %d trade(s) executed.", STATE.trade_count)
        summary = await llm.regime_summary(list(_decision_log))
        log.info("Regime summary:\n%s", summary)

    log.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
