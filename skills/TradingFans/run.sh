#!/usr/bin/env bash
# TradingFans — OpenClaw skill launcher
# ============================================================
# Enforces env vars, bootstraps a Python 3.11 venv,
# installs dependencies on first run, then executes the engine.
# ============================================================
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_DIR="$SKILL_DIR/src"
VENV="$SKILL_DIR/.venv"

# ── Dotenv auto-load FIRST (before guard check) ───────────────
ENV_FILE="$(dirname "$(dirname "$SKILL_DIR")")/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# ── Required env var guard ─────────────────────────────────────
REQUIRED_VARS=(POLY_PRIVATE_KEY POLY_FUNDER OPENAI_API_KEY)
MISSING=()
for var in "${REQUIRED_VARS[@]}"; do
  if [[ -z "${!var:-}" ]]; then
    MISSING+=("$var")
  fi
done
if [[ ${#MISSING[@]} -gt 0 ]]; then
  echo "❌  Missing required environment variables:" >&2
  for var in "${MISSING[@]}"; do
    echo "    • $var" >&2
  done
  echo "" >&2
  echo "   Set them in your shell, a .env file, or via OpenClaw secrets." >&2
  echo "   See SKILL.md for full configuration reference." >&2
  exit 1
fi

# ── Python 3.11 discovery ──────────────────────────────────────
find_python() {
  # Explicit Windows AppData paths first (avoids Windows Store alias)
  local win_candidates=(
    "$LOCALAPPDATA/Programs/Python/Python313/python.exe"
    "$LOCALAPPDATA/Programs/Python/Python312/python.exe"
    "$LOCALAPPDATA/Programs/Python/Python311/python.exe"
    "$LOCALAPPDATA/Programs/Python/Python310/python.exe"
  )
  for candidate in "${win_candidates[@]}"; do
    if [[ -f "$candidate" ]]; then
      echo "$candidate"
      return 0
    fi
  done
  # Standard PATH candidates (skip Windows Store aliases)
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
      local ver
      ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null) || continue
      # Skip if it's a Windows Store stub (outputs nothing or errors)
      if [[ -n "$ver" ]]; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PY=$(find_python || true)
if [[ -z "$PY" ]]; then
  echo "❌  Python 3.11 not found. Install it: https://python.org/downloads/" >&2
  exit 1
fi

PY_VER=$("$PY" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if [[ "$PY_VER" < "3.10" ]]; then
  echo "⚠️   Python $PY_VER detected — 3.10+ required." >&2
fi

# ── Virtualenv bootstrap ───────────────────────────────────────
if [[ ! -d "$VENV" ]]; then
  echo "📦  Creating virtualenv at $VENV ..."
  "$PY" -m venv "$VENV"
fi

# Activate (cross-platform: bash on Windows uses Scripts/)
if [[ -f "$VENV/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
elif [[ -f "$VENV/Scripts/activate" ]]; then
  # shellcheck disable=SC1091
  source "$VENV/Scripts/activate"
else
  echo "❌  Could not activate virtualenv at $VENV" >&2
  exit 1
fi

# ── Dependency install (cached by .installed sentinel) ────────
REQ="$SRC_DIR/tradingfans/requirements.txt"
SENTINEL="$VENV/.installed"
if [[ ! -f "$SENTINEL" ]] || [[ "$REQ" -nt "$SENTINEL" ]]; then
  echo "📦  Installing Python dependencies..."
  pip install -q --upgrade pip
  pip install -q -r "$REQ"
  touch "$SENTINEL"
  echo "✅  Dependencies installed."
fi

# ── Launch ─────────────────────────────────────────────────────
echo "🚀  TradingFans starting (POLY_DRY_RUN=${POLY_DRY_RUN:-0}) ..."
echo "    Max size : ${POLY_MAX_SIZE:-10} USDC"
echo "    Symbol   : ${POLY_SYMBOL:-both}"
echo "    Chain    : ${POLY_CHAIN_ID:-137 (Polygon mainnet)}"
echo ""

export PYTHONPATH="$SRC_DIR"
exec python -m tradingfans.engine "$@"
