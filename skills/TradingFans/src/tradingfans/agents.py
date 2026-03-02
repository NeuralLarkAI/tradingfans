"""
agents.py — Lightweight multi-agent manager (max 5) for DRY RUN experimentation.

This does NOT spawn new OS processes. Agents are logical configs that share STATE memory.

Design:
  - Up to MAX_AGENTS agents active at once.
  - Each market is deterministically assigned to exactly one agent (hash(market_id) mod N),
    so agents can be compared without duplicating orders on the same market.
  - Each agent has bounded knobs (min_edge, weights, sizing).
  - "Evolution" is bounded: we can spawn mutated variants from the current best agent.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, asdict


MAX_AGENTS = 5


@dataclass
class AgentConfig:
    agent_id: str
    name: str
    brain: str  # "rules" | "llm"

    # Decision model knobs
    min_edge: float
    w_m1: float
    w_m5: float
    w_vol: float

    # Sizing knobs
    max_size_usdc: float
    min_order_size_usdc: float
    edge_full_scale: float

    created_epoch: float


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(v)))


def default_pool() -> list[AgentConfig]:
    now = time.time()
    pool = [
        AgentConfig(
            agent_id="a_momo",
            name="Momentum",
            brain="rules",
            min_edge=0.006,
            w_m1=20.0,
            w_m5=8.0,
            w_vol=-4.0,
            max_size_usdc=75.0,
            min_order_size_usdc=10.0,
            edge_full_scale=0.05,
            created_epoch=now,
        ),
        AgentConfig(
            agent_id="a_mean",
            name="MeanRev",
            brain="rules",
            min_edge=0.006,
            w_m1=-14.0,
            w_m5=-6.0,
            w_vol=-2.0,
            max_size_usdc=75.0,
            min_order_size_usdc=10.0,
            edge_full_scale=0.06,
            created_epoch=now,
        ),
        AgentConfig(
            agent_id="a_cons",
            name="Conservative",
            brain="rules",
            min_edge=0.012,
            w_m1=14.0,
            w_m5=6.0,
            w_vol=-6.0,
            max_size_usdc=40.0,
            min_order_size_usdc=10.0,
            edge_full_scale=0.05,
            created_epoch=now,
        ),
    ]
    # Optional LLM agent (disabled unless POLY_LLM_TRADER=1; see llm_trader.py).
    if os.environ.get("OPENAI_API_KEY", "").strip():
        pool.append(
            AgentConfig(
                agent_id="a_llm",
                name="LLM",
                brain="llm",
                min_edge=0.008,
                w_m1=0.0,
                w_m5=0.0,
                w_vol=0.0,
                max_size_usdc=60.0,
                min_order_size_usdc=10.0,
                edge_full_scale=0.05,
                created_epoch=now,
            )
        )
    return pool[:MAX_AGENTS]


def maybe_enable_multi_agent(dry_run: bool) -> bool:
    """
    Default: ON in dry-run, OFF in live (unless explicitly enabled).
    """
    env = os.environ.get("POLY_MULTI_AGENT", "").strip()
    if env != "":
        return env in ("1", "true", "True", "yes", "YES", "on", "ON")
    return bool(dry_run)


def mutate(base: AgentConfig, *, seed: int | None = None) -> AgentConfig:
    rng = random.Random(seed if seed is not None else int(time.time() * 1000) % 2**31)
    now = time.time()
    rid = f"a_mut_{int(now)}_{rng.randrange(1000,9999)}"

    def j(x: float, pct: float) -> float:
        return x * (1.0 + rng.uniform(-pct, pct))

    cfg = AgentConfig(
        agent_id=rid,
        name=f"Mut({base.name})",
        brain=base.brain,
        min_edge=_clamp(j(base.min_edge, 0.35), 0.001, 0.050),
        w_m1=_clamp(j(base.w_m1, 0.35), -40.0, 40.0),
        w_m5=_clamp(j(base.w_m5, 0.35), -20.0, 20.0),
        w_vol=_clamp(j(base.w_vol, 0.35), -20.0, 20.0),
        max_size_usdc=_clamp(j(base.max_size_usdc, 0.35), 5.0, 200.0),
        min_order_size_usdc=_clamp(j(base.min_order_size_usdc, 0.25), 1.0, 50.0),
        edge_full_scale=_clamp(j(base.edge_full_scale, 0.30), 0.01, 0.20),
        created_epoch=now,
    )
    return cfg


def to_public_dict(cfg: AgentConfig) -> dict:
    d = asdict(cfg)
    # keep payload stable
    d["min_edge"] = round(float(d["min_edge"]), 4)
    d["w_m1"] = round(float(d["w_m1"]), 3)
    d["w_m5"] = round(float(d["w_m5"]), 3)
    d["w_vol"] = round(float(d["w_vol"]), 3)
    d["max_size_usdc"] = round(float(d["max_size_usdc"]), 2)
    d["min_order_size_usdc"] = round(float(d["min_order_size_usdc"]), 2)
    d["edge_full_scale"] = round(float(d["edge_full_scale"]), 4)
    return d


def assign_agent_id(market_id: str, agent_ids: list[str]) -> str:
    # Stable deterministic assignment.
    if not agent_ids:
        return ""
    idx = (abs(hash(market_id)) % len(agent_ids))
    return agent_ids[idx]
