"""
tuner.py — Bounded, allowlisted self-optimization.

Design goals:
  - Only adjust a small allowlist of numeric knobs.
  - Enforce min/max bounds and small step sizes.
  - Persist changes to a local JSON file for continuity.
  - Stream every change into STATE so the dashboard can show it live.

This is intentionally conservative: it optimizes trade frequency + capital usage
in DRY RUN, and is disabled by default in live mode.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .state import STATE, SignalRecord


@dataclass(frozen=True)
class ParamSpec:
    key: str
    default: float
    min_v: float
    max_v: float
    step: float
    description: str
    live_allowed: bool = False

    def clamp(self, v: float) -> float:
        return max(self.min_v, min(self.max_v, float(v)))

    def quantize(self, v: float) -> float:
        v = self.clamp(v)
        if self.step <= 0:
            return v
        # snap to nearest step from min_v
        n = round((v - self.min_v) / self.step)
        return self.clamp(self.min_v + n * self.step)


SPECS: dict[str, ParamSpec] = {
    "decision.min_edge": ParamSpec(
        key="decision.min_edge",
        default=0.02,
        min_v=0.001,
        max_v=0.06,
        step=0.001,
        description="Minimum |edge| threshold to emit BUY_YES/BUY_NO.",
        live_allowed=False,
    ),
    "engine.max_size_usdc": ParamSpec(
        key="engine.max_size_usdc",
        default=10.0,
        min_v=1.0,
        max_v=50.0,
        step=0.5,
        description="Maximum order size (USDC) per trade.",
        live_allowed=False,
    ),
    "engine.poll_interval_sec": ParamSpec(
        key="engine.poll_interval_sec",
        default=5.0,
        min_v=2.0,
        max_v=15.0,
        step=0.5,
        description="Seconds between market scans.",
        live_allowed=True,
    ),
    "engine.min_order_size_usdc": ParamSpec(
        key="engine.min_order_size_usdc",
        default=1.00,
        min_v=1.00,
        max_v=20.00,
        step=0.50,
        description="Minimum order size (USDC) required to submit a trade.",
        live_allowed=True,
    ),
    "engine.edge_full_scale": ParamSpec(
        key="engine.edge_full_scale",
        default=0.05,
        min_v=0.01,
        max_v=0.20,
        step=0.005,
        description="Sizing scale: edge_full_scale maps to max_size (sqrt sizing).",
        live_allowed=False,
    ),
    "risk.max_time_to_expiry_sec": ParamSpec(
        key="risk.max_time_to_expiry_sec",
        default=600.0,
        min_v=300.0,
        max_v=7200.0,
        step=300.0,
        description="Maximum seconds-to-expiry allowed (risk time filter).",
        live_allowed=False,
    ),
    "tuner.target_trades_per_hour": ParamSpec(
        key="tuner.target_trades_per_hour",
        default=12.0,
        min_v=0.0,
        max_v=60.0,
        step=1.0,
        description="Target executed trades per hour (DRY-run).",
        live_allowed=True,
    ),
    "tuner.interval_sec": ParamSpec(
        key="tuner.interval_sec",
        default=30.0,
        min_v=10.0,
        max_v=120.0,
        step=5.0,
        description="Seconds between autotune evaluations.",
        live_allowed=True,
    ),
}


# The knobs the agent is allowed to change automatically.
AUTO_ALLOWLIST: tuple[str, ...] = (
    "decision.min_edge",
    "engine.max_size_usdc",
    "engine.poll_interval_sec",
    "engine.min_order_size_usdc",
    "risk.max_time_to_expiry_sec",
)


def _project_root() -> Path:
    # .../skills/TradingFans/src/tradingfans/tuner.py -> parents[2] = skills/TradingFans
    return Path(__file__).resolve().parents[2]


def config_path() -> Path:
    return _project_root() / "config" / "tuner.json"


def _load_file(path: Path) -> dict:
    try:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _enabled_by_default(dry_run: bool) -> bool:
    # Default ON for dry-run, OFF for live.
    return dry_run


def init_from_config(*, dry_run: bool) -> None:
    """
    Load tuner config (if present) and hydrate STATE.{min_edge,max_size,poll_interval}.
    Does not start the autotune loop; engine is responsible for that.
    """
    path = config_path()
    cfg = _load_file(path)

    enabled_env = os.environ.get("POLY_TUNER", "").strip()
    if enabled_env != "":
        enabled = enabled_env in ("1", "true", "True", "yes", "YES", "on", "ON")
    else:
        enabled = _enabled_by_default(dry_run)

    live_allowed = os.environ.get("POLY_TUNER_LIVE", "0") == "1"
    if not dry_run and not live_allowed:
        enabled = False

    def get(key: str) -> float:
        spec = SPECS[key]
        params_in = cfg.get("params", {})
        if key in params_in:
            raw = params_in.get(key)
        else:
            # Dry-run sensible defaults when no config exists yet.
            if dry_run and key == "engine.min_order_size_usdc":
                raw = 1.00
            elif dry_run and key == "risk.max_time_to_expiry_sec":
                raw = 7200.0
            else:
                raw = spec.default
        return spec.quantize(raw)

    params = {k: get(k) for k in SPECS.keys()}

    STATE.tuner_config_path = str(path)
    STATE.tuner_allowlist = list(AUTO_ALLOWLIST)
    STATE.tuner_params = params
    STATE.tuner_specs = [
        {
            "key": spec.key,
            "default": spec.default,
            "min": spec.min_v,
            "max": spec.max_v,
            "step": spec.step,
            "description": spec.description,
            "live_allowed": spec.live_allowed,
            "auto": spec.key in AUTO_ALLOWLIST,
        }
        for spec in SPECS.values()
    ]
    STATE.tuner_enabled = bool(enabled)
    STATE.tuner_status = "ON" if enabled else "OFF"

    # Apply into runtime knobs.
    STATE.min_edge = params["decision.min_edge"]
    STATE.max_size = params["engine.max_size_usdc"]
    STATE.poll_interval = params["engine.poll_interval_sec"]
    STATE.min_order_size = params["engine.min_order_size_usdc"]
    STATE.edge_full_scale = params["engine.edge_full_scale"]
    STATE.max_time_to_expiry = params["risk.max_time_to_expiry_sec"]


def persist_current() -> None:
    """Persist STATE.tuner_params to disk."""
    path = Path(STATE.tuner_config_path) if STATE.tuner_config_path else config_path()
    payload = {
        "updated_at_epoch": time.time(),
        "params": dict(STATE.tuner_params),
    }
    _atomic_write_json(path, payload)


def set_param(key: str, value: float, *, source: str = "local") -> tuple[bool, float | None, float | None, str]:
    """
    Set a tunable parameter with bounds checking.
    Returns: (ok, old, new, message)
    """
    if key not in SPECS:
        return False, None, None, f"Unknown key: {key}"
    spec = SPECS[key]
    if not STATE.dry_run and not spec.live_allowed:
        return False, None, None, f"{key} not allowed in live mode"

    old = float(STATE.tuner_params.get(key, spec.default))
    new = spec.quantize(value)
    if new == old:
        return True, old, new, "no_change"

    _apply_param(key, new)
    _emit_event(key, old, new, f"manual_set:{source}", {"source": source})
    return True, old, new, "ok"


def _emit_event(key: str, old: float, new: float, reason: str, metrics: dict) -> None:
    STATE.tuner_events.appendleft({
        "ts_epoch": time.time(),
        "key": key,
        "old": old,
        "new": new,
        "reason": reason,
        "metrics": metrics,
    })


def _recent_executed_trades(within_sec: float) -> list[SignalRecord]:
    now = time.time()
    out: list[SignalRecord] = []
    for s in list(STATE.recent_signals):
        if s.signal == "NO_TRADE":
            continue
        if not s.order_id:
            continue
        if STATE.dry_run and s.order_id != "dry-run":
            continue
        if (now - s.ts_epoch) <= within_sec:
            out.append(s)
    return out


def _apply_param(key: str, value: float) -> None:
    STATE.tuner_params[key] = value
    if key == "decision.min_edge":
        STATE.min_edge = value
    elif key == "engine.max_size_usdc":
        STATE.max_size = value
    elif key == "engine.poll_interval_sec":
        STATE.poll_interval = value
    elif key == "engine.min_order_size_usdc":
        STATE.min_order_size = value
    elif key == "risk.max_time_to_expiry_sec":
        STATE.max_time_to_expiry = value
    elif key == "engine.edge_full_scale":
        STATE.edge_full_scale = value


def autotune_step() -> None:
    """
    One conservative tuning step.
    Objective: keep trade frequency near target while avoiding over-deploying dry-run balance.
    """
    params = STATE.tuner_params
    if not params:
        return

    trades_10m = _recent_executed_trades(600.0)
    trade_n = len(trades_10m)
    avg_abs_edge = (sum(abs(t.edge) for t in trades_10m) / trade_n) if trade_n else 0.0

    target_per_hour = params.get("tuner.target_trades_per_hour", SPECS["tuner.target_trades_per_hour"].default)
    target_10m = max(0.0, float(target_per_hour) / 6.0)

    deployed = float(STATE.dry_deployed) if STATE.dry_run else 0.0
    available = max(0.0, float(STATE.DRY_START_BALANCE) - deployed) if STATE.dry_run else 0.0
    avail_ratio = (available / float(STATE.DRY_START_BALANCE)) if STATE.dry_run else 0.0

    metrics = {
        "trades_10m": trade_n,
        "avg_abs_edge": round(avg_abs_edge, 5),
        "target_10m": round(target_10m, 3),
        "avail_ratio": round(avail_ratio, 3),
        "scan_count": STATE.scan_count,
    }

    # 1) Tune min_edge to hit target trade frequency.
    min_edge_spec = SPECS["decision.min_edge"]
    min_edge = float(params.get("decision.min_edge", min_edge_spec.default))

    if target_10m > 0 and trade_n < max(1.0, target_10m * 0.50):
        new = min_edge_spec.quantize(min_edge - min_edge_spec.step)
        if new != min_edge:
            _apply_param("decision.min_edge", new)
            _emit_event("decision.min_edge", min_edge, new, "trade_rate_low", metrics)
    elif target_10m > 0 and trade_n > target_10m * 1.50 and avg_abs_edge < (min_edge * 1.25):
        new = min_edge_spec.quantize(min_edge + min_edge_spec.step)
        if new != min_edge:
            _apply_param("decision.min_edge", new)
            _emit_event("decision.min_edge", min_edge, new, "trade_rate_high_low_edge", metrics)

    # 2) Tune max_size to avoid over-deploying the dry balance.
    size_spec = SPECS["engine.max_size_usdc"]
    max_size = float(params.get("engine.max_size_usdc", size_spec.default))
    if STATE.dry_run:
        if avail_ratio < 0.20 and trade_n > 0:
            new = size_spec.quantize(max_size * 0.90)
            if new != max_size:
                _apply_param("engine.max_size_usdc", new)
                _emit_event("engine.max_size_usdc", max_size, new, "available_low_reduce_size", metrics)
        elif avail_ratio > 0.80 and trade_n < max(1.0, target_10m * 0.50) and avg_abs_edge >= min_edge:
            new = size_spec.quantize(max_size * 1.10)
            if new != max_size:
                _apply_param("engine.max_size_usdc", new)
                _emit_event("engine.max_size_usdc", max_size, new, "available_high_increase_size", metrics)

    # 3) Tune poll_interval slightly if we are not seeing markets (very conservative).
    poll_spec = SPECS["engine.poll_interval_sec"]
    poll = float(params.get("engine.poll_interval_sec", poll_spec.default))
    if STATE.scan_count > 0 and trade_n == 0 and target_10m > 0:
        # If no trades for a while, scan a bit faster (down to min).
        new = poll_spec.quantize(poll - poll_spec.step)
        if new != poll:
            _apply_param("engine.poll_interval_sec", new)
            _emit_event("engine.poll_interval_sec", poll, new, "no_trades_scan_faster", metrics)

    STATE.tuner_last_run = time.time()


async def autotune_loop() -> None:
    """Run autotune_step() periodically and persist config after changes."""
    while True:
        try:
            if not STATE.tuner_enabled:
                STATE.tuner_status = "OFF"
                await _sleep(2.0)
                continue

            STATE.tuner_status = "ON"
            before = json.dumps(STATE.tuner_params, sort_keys=True)
            autotune_step()
            after = json.dumps(STATE.tuner_params, sort_keys=True)
            if before != after:
                persist_current()
        except Exception as exc:
            STATE.tuner_status = f"ERR: {type(exc).__name__}"
        await _sleep(float(STATE.tuner_params.get("tuner.interval_sec", SPECS["tuner.interval_sec"].default)))


async def _sleep(sec: float) -> None:
    # Tiny helper to keep imports minimal and allow easier testing.
    import asyncio

    await asyncio.sleep(max(0.25, float(sec)))
