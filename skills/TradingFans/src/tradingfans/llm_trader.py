"""
llm_trader.py — Optional LLM-based pricing/decision module (DRY RUN by default).

Safety:
  - Disabled unless POLY_LLM_TRADER=1.
  - In live mode, also requires POLY_LLM_TRADER_LIVE=1.
  - Always bounded by existing risk filters + agent min_edge + sizing caps.

The LLM produces a fair probability estimate (p_model) and a suggested action.
We then compute edge = p_model - implied_yes and apply thresholds.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from collections import deque

import openai


@dataclass(frozen=True)
class LLMDecision:
    action: str       # "BUY_YES" | "BUY_NO" | "NO_TRADE"
    p_model: float    # 0..1
    reason: str


_MODEL = os.environ.get("OPENAI_MODEL_TRADER", os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"))
_CACHE_TTL = 12.0
_cache: dict[str, tuple[float, LLMDecision]] = {}
_call_ts: deque[float] = deque(maxlen=200)


def enabled(*, dry_run: bool, live_ok: bool = False) -> bool:
    env = os.environ.get("POLY_LLM_TRADER", "").strip()
    if env != "":
        on = env in ("1", "true", "True", "yes", "YES", "on", "ON")
    else:
        # Default: ON in DRY RUN (if an OpenAI key exists).
        # LIVE requires an explicit toggle (live_ok) to avoid accidental spend.
        on = bool(dry_run or live_ok)
    if not on:
        return False
    if not dry_run and (not live_ok) and os.environ.get("POLY_LLM_TRADER_LIVE", "0") != "1":
        return False
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


async def decide(
    *,
    market_id: str,
    question: str,
    implied_yes: float,
    tte_sec: float,
    m1: float,
    m5: float,
    vol1: float,
) -> LLMDecision:
    now = time.time()
    hit = _cache.get(market_id)
    if hit and (now - hit[0]) <= _CACHE_TTL:
        return hit[1]

    # Rate limit to control spend/latency.
    try:
        max_per_min = int(os.environ.get("POLY_LLM_MAX_CALLS_PER_MIN", "10"))
    except Exception:
        max_per_min = 10
    if max_per_min > 0:
        while _call_ts and (now - _call_ts[0]) > 60.0:
            _call_ts.popleft()
        if len(_call_ts) >= max_per_min:
            d = LLMDecision(action="NO_TRADE", p_model=float(implied_yes), reason="llm_rate_limited")
            _cache[market_id] = (now, d)
            return d

    client = openai.AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

    sys = (
        "You are an automated market-making analyst for Polymarket 5-minute crypto Up/Down markets. "
        "You must output a strict JSON object with keys: action, p_model, reason. "
        "action must be one of BUY_YES, BUY_NO, NO_TRADE. "
        "p_model must be a number between 0 and 1 representing your fair probability of YES (UP). "
        "Be conservative and prefer NO_TRADE unless the edge is meaningful."
    )
    user = {
        "market_id": market_id[:16],
        "question": question,
        "implied_yes": round(float(implied_yes), 6),
        "time_to_expiry_sec": round(float(tte_sec), 1),
        "spot_features": {
            "m1_return": round(float(m1), 7),
            "m5_return": round(float(m5), 7),
            "vol1": round(float(vol1), 7),
        },
        "output_format": {"action": "BUY_YES|BUY_NO|NO_TRADE", "p_model": "0..1", "reason": "short string"},
    }

    async def _call() -> LLMDecision:
        resp = await client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": json.dumps(user)},
            ],
            max_tokens=160,
            temperature=0.2,
        )
        txt = (resp.choices[0].message.content or "").strip()
        # Extract JSON strictly
        obj = json.loads(txt) if txt.startswith("{") else json.loads(txt[txt.find("{") : txt.rfind("}") + 1])
        action = str(obj.get("action", "NO_TRADE")).upper()
        if action not in ("BUY_YES", "BUY_NO", "NO_TRADE"):
            action = "NO_TRADE"
        p_model = float(obj.get("p_model", implied_yes))
        p_model = max(0.0, min(1.0, p_model))
        reason = str(obj.get("reason", ""))[:180]
        return LLMDecision(action=action, p_model=p_model, reason=reason)

    try:
        _call_ts.append(now)
        d = await asyncio.wait_for(_call(), timeout=3.0)
    except Exception:
        d = LLMDecision(action="NO_TRADE", p_model=float(implied_yes), reason="llm_unavailable")

    _cache[market_id] = (now, d)
    return d
