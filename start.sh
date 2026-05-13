#!/usr/bin/env bash
set -e
REPO="$(cd "$(dirname "$0")" && pwd)"

# ── 1. Copy .env if missing ──────────────────────────────────────────────────
if [ ! -f "$REPO/config/.env" ]; then
  cp "$REPO/config/.env.example" "$REPO/config/.env"
  echo "[start.sh] Created config/.env from example — edit it to add API keys."
fi

# ── 2. Build Rust binary if missing ─────────────────────────────────────────
RUST_BIN="$REPO/rust-core/target/release/arb"
if [ ! -f "$RUST_BIN" ]; then
  echo "[start.sh] Rust binary not found — building (this takes ~2 min first time)..."
  cd "$REPO/rust-core" && cargo build --release
  cd "$REPO"
fi

# ── 3. Disable SSL verification if behind a proxy ───────────────────────────
# Set DISABLE_SSL=1 in config/.env to suppress cert errors on corporate networks.
if grep -q "^DISABLE_SSL=1" "$REPO/config/.env" 2>/dev/null; then
  export PYTHONHTTPSVERIFY=0
  export CURL_CA_BUNDLE=""
fi

# ── 4. Run backfill if markets.json is empty / missing ──────────────────────
MARKETS="$REPO/config/markets.json"
PAIR_COUNT=0
if [ -f "$MARKETS" ]; then
  PAIR_COUNT=$(python3 -c "import json,sys; d=json.load(open('$MARKETS')); print(len(d.get('pairs',[])))" 2>/dev/null || echo 0)
fi
if [ "$PAIR_COUNT" -eq 0 ]; then
  echo "[start.sh] No market pairs found — running backfill..."
  uv run --directory "$REPO/python-core" python "$REPO/scripts/backfill_matches.py"
fi

# ── 5. Launch bot ────────────────────────────────────────────────────────────
echo "[start.sh] Starting PolyyKing..."
exec uv run --directory "$REPO/python-core" python main.py
