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
                k = k.strip()
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


def _size_usdc(edge: float, max_size: float) -> float:
    scale = max(0.001, float(getattr(STATE, "edge_full_scale", 0.05)))
    # Convex sizing: small edges still get meaningful size, while large edges saturate at max_size.
    ratio = max(0.0, abs(edge) / scale)
    fraction = min(ratio ** 0.5, 1.0)
    return round(max(fraction * max_size, 0.0), 2)


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
    max_size: float,
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

    book = await loop.run_in_executor(None, clob.get_book, market.yes_token_id)
    implied_yes: float | None = book.mid if book else None
    if implied_yes is None:
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

    decision = decision_compute(
        window=window,
        implied_yes=implied_yes,
        min_edge=STATE.min_edge,
        w_m1=float(STATE.strategy.get("w_m1", 20.0)),
        w_m5=float(STATE.strategy.get("w_m5", 8.0)),
        w_vol=float(STATE.strategy.get("w_vol", -4.0)),
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

    size = max(_size_usdc(decision.edge, max_size), STATE.min_order_size)
    if size < STATE.min_order_size:
        log.debug("Order size %.2f USDC < minimum %.2f — skipping.", size, STATE.min_order_size)
        return None

    if decision.signal == "BUY_YES":
        token_id = market.yes_token_id
        price = book.best_ask if book.best_ask else implied_yes
    else:
        token_id = market.no_token_id
        price = 1.0 - (book.best_bid if book.best_bid else implied_yes)
        price = max(0.01, min(price, 0.99))

    log.info(
        "🔔 TRADE | %-8s | size=%.2f USDC | price=%.4f | edge=%+.4f | %s",
        decision.signal, size, price, decision.edge, market.question[:55],
    )

    order_id = await loop.run_in_executor(
        None, clob.place_order, token_id, decision.signal, size, price
    )

    if order_id:
        _open_positions.add(market.market_id)
        STATE.trade_count += 1
        if STATE.dry_run:
            STATE.dry_deployed += size   # track virtual capital committed
            # Track trade for later resolution / realized PnL
            record_open_trade(OpenTrade(
                market_id=market.market_id,
                question=market.question,
                symbol=symbol,
                side=decision.signal,
                size_usdc=size,
                price_paid=price,
                entry_epoch=time.time(),
                end_epoch=market.end_time.timestamp(),
            ))
        # Update the last signal record with the real order_id
        if STATE.recent_signals:
            sr = STATE.recent_signals[0]
            STATE.recent_signals[0] = SignalRecord(
                ts=sr.ts, signal=sr.signal, edge=sr.edge, p_model=sr.p_model,
                implied_yes=sr.implied_yes, m1=sr.m1, m5=sr.m5, vol1=sr.vol1,
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

    POLYGON_RPC = "https://polygon-rpc.com"
    USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e on Polygon

    try:
        addr_raw = (address[2:] if address.startswith("0x") else address).lower()
        calldata = "0x70a08231" + addr_raw.zfill(64)   # balanceOf(address)

        async with _aiohttp.ClientSession() as sess:
            # USDC balance
            r1 = await sess.post(POLYGON_RPC, json={
                "jsonrpc": "2.0", "method": "eth_call",
                "params": [{"to": USDC_CONTRACT, "data": calldata}, "latest"],
                "id": 1,
            }, timeout=_aiohttp.ClientTimeout(total=8))
            hex_u = (await r1.json()).get("result", "0x0")
            usdc = int(hex_u, 16) / 1_000_000   # 6 decimals

            # MATIC balance
            r2 = await sess.post(POLYGON_RPC, json={
                "jsonrpc": "2.0", "method": "eth_getBalance",
                "params": [address, "latest"],
                "id": 2,
            }, timeout=_aiohttp.ClientTimeout(total=8))
            hex_m = (await r2.json()).get("result", "0x0")
            matic = int(hex_m, 16) / 1e18

        return usdc, matic
    except Exception as exc:
        log.debug("Wallet fetch failed: %s", exc)
        return None, None


async def wallet_poll_loop() -> None:
    """Poll Polygon for wallet USDC/MATIC balance every 60 seconds."""
    while True:
        usdc, matic = await _fetch_wallet_balance()
        if usdc is not None:
            STATE.wallet_usdc = usdc
            STATE.wallet_matic = matic
            log.debug("Wallet: USDC=%.2f MATIC=%.4f", usdc, matic)
        await asyncio.sleep(60)


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
        "Trading loop live | max_size=%.2f USDC | poll=%.1fs | symbol=%s",
        STATE.max_size, STATE.poll_interval, symbol_filter,
    )

    while True:
        try:
            max_size = STATE.max_size
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
                if book:
                    active.append(ActiveMarket(
                        market_id=m.market_id,
                        question=m.question,
                        symbol=sym,
                        tte=m.time_to_expiry,
                        implied_yes=book.mid,
                        spread=book.spread,
                        depth_ok=check_depth(book, book.mid),
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
                tasks = [
                    _evaluate_market(m, spot, clob, max_size, symbol_filter, loop)
                    for m in markets
                ]
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

    # Start subsystems
    ui_runner = await start_server()
    await spot.start()
    await llm.start()

    log.info("Warming up spot feed (3s)...")
    await asyncio.sleep(3.0)

    trade_task = asyncio.create_task(
        trading_loop(gamma, spot, clob, llm, sym_filter),
        name="trading-loop",
    )
    wallet_task = asyncio.create_task(
        wallet_poll_loop(),
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

    await stop_event.wait()
    log.info("Shutting down...")

    for task in (trade_task, wallet_task, tuner_task, remote_task):
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
