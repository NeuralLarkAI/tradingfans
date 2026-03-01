---
name: trading-fans
description: Polymarket 5-minute crypto Up/Down trading agent. Momentum edge model, fee shield, and strict risk guardrails. NO LLM in the trade loop.
version: 1.0.0
homepage: https://github.com/polymarket/tradingfans
user-invocable: true
command-dispatch: tool
command-tool: Exec
command-arg-mode: raw
metadata: '{"requiresEnv":["POLY_PRIVATE_KEY","POLY_FUNDER","OPENAI_API_KEY"],"requiresPython":"3.11","tags":["trading","polymarket","crypto","quantitative"]}'
---

# TradingFans

Runs a fully autonomous Polymarket 5-minute crypto Up/Down trading agent.

The agent discovers active 5-minute BTC/ETH markets via the Gamma API, streams
real-time prices from Binance WebSocket, and executes orders when a quantitative
momentum edge model detects ≥ 2% edge over the implied market probability.

**The LLM is NEVER in the trade-execution loop.** OpenAI is used only for
asynchronous post-trade commentary and market regime narration.

---

## Usage

```
/TradingFans [--dry-run] [--max-size <USDC>] [--symbol BTC|ETH|both]
```

| Flag | Default | Description |
|---|---|---|
| `--dry-run` | off | Log trades without placing real orders |
| `--max-size` | 10 | Max USDC per order |
| `--symbol` | both | Restrict to BTC, ETH, or both |

---

## Architecture

```
GammaCache  ──► discover active 5m crypto markets (refresh 60s)
SpotFeed    ──► Binance WS aggTrade (BTC + ETH, rolling 5m window)
ClobClient  ──► orderbook reads + order placement (py-clob-client)
decision    ──► p_model via logistic(m1, m5, vol1); edge = p_model - implied_yes
risk        ──► all guardrails (time, freshness, spread, depth, no-trade zone)
LLMAdvisor  ──► async post-trade commentary (NEVER blocks trade loop)
engine      ──► asyncio orchestrator, graceful shutdown on SIGINT/SIGTERM
```

---

## Guardrails

| Check | Rule |
|---|---|
| Time filter | `0 < time_to_expiry < 600 seconds` |
| Spot freshness | Last Binance tick ≤ 2 seconds ago |
| No-trade zone | Implied YES **not** in `[0.45, 0.55]` |
| Max spread | ≤ 3% |
| Min depth | ≥ $100 USDC notional near mid |
| Min edge | `abs(edge) ≥ 0.02` |

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `POLY_PRIVATE_KEY` | ✅ | Polymarket EOA private key (hex, with or without 0x prefix) |
| `POLY_FUNDER` | ✅ | Polymarket funder address (USDC source wallet) |
| `OPENAI_API_KEY` | ✅ | Used for post-trade LLM commentary only |
| `POLY_MAX_SIZE` | optional | Max USDC per order (default: `10`) |
| `POLY_CHAIN_ID` | optional | `137` = Polygon mainnet (default), `80002` = Amoy testnet |
| `POLY_DRY_RUN` | optional | Set `1` to log without placing real orders |
| `POLY_SYMBOL` | optional | `BTC`, `ETH`, or `both` (default: `both`) |
| `POLY_POLL_INTERVAL` | optional | Seconds between market scans (default: `5`) |
| `OPENAI_MODEL` | optional | OpenAI model for commentary (default: `gpt-4.1-mini`) |

---

## Decision Model

```
m1       = (price_now - price_1m_ago) / price_1m_ago
m5       = (price_now - price_5m_ago) / price_5m_ago
vol1     = std(log_returns over last 60s)

logit    = 20*m1 + 8*m5 - 4*vol1
p_model  = sigmoid(logit)          # model's P(YES wins)
edge     = p_model - implied_yes   # alpha

signal:
  edge > +0.02  →  BUY YES
  edge < -0.02  →  BUY NO
  otherwise     →  NO_TRADE
```

---

## ⚠️ Risk Disclaimer

This skill places real money on Polymarket prediction markets.
Start with `--dry-run` and `--max-size 1` before live trading.
Past performance of the edge model is not indicative of future results.
