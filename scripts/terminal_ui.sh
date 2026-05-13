#!/usr/bin/env bash
# terminal_ui.sh — launch PolyyKing bot inside a Moulti terminal dashboard
#
# Usage:
#   bash scripts/terminal_ui.sh      # direct
#   bash start.sh --moulti           # via start.sh flag
#   MOULTI=1 bash start.sh           # via env var
set -e

REPO="$(cd "$(dirname "$0")/.." && pwd)"

# ── Dependency checks ───────────────────────────────────────────────────────
if ! command -v moulti &>/dev/null; then
  echo "[terminal_ui.sh] moulti not found." >&2
  echo "[terminal_ui.sh] Install with: pipx install moulti" >&2
  exit 1
fi

VENV_PYTHON="$REPO/python-core/.venv/bin/python"
if [ ! -f "$VENV_PYTHON" ]; then
  echo "[terminal_ui.sh] Python venv not found at $VENV_PYTHON" >&2
  echo "[terminal_ui.sh] Run: cd '$REPO/python-core' && uv sync" >&2
  exit 1
fi

ROUTER="$REPO/scripts/moulti_router.py"
if [ ! -f "$ROUTER" ]; then
  echo "[terminal_ui.sh] Router script not found: $ROUTER" >&2
  exit 1
fi

# ── Launch ──────────────────────────────────────────────────────────────────
# moulti run wraps the router in a Moulti TUI instance and sets
# MOULTI_SOCKET_PATH so all `moulti step` / `moulti pass` calls
# connect to this instance automatically.
echo "[terminal_ui.sh] Starting PolyyKing terminal dashboard..."
exec moulti run -- "$VENV_PYTHON" "$ROUTER" "$REPO"
