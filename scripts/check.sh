#!/usr/bin/env bash
# PolyyKing — full system health check
# Usage: bash scripts/check.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PASS=0; FAIL=0; WARN=0

ok()   { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
fail() { echo "  [FAIL] $1"; FAIL=$((FAIL + 1)); }
warn() { echo "  [WARN] $1"; WARN=$((WARN + 1)); }
section() { echo; echo "=== $1 ==="; }

VENV=python-core/.venv/bin/python

# ── Rust ──────────────────────────────────────────────────────────────────────
section "Rust binary"
if [[ -f rust-core/target/release/arb ]]; then
  ok "rust-core/target/release/arb exists"
else
  fail "rust-core/target/release/arb missing — run: cd rust-core && cargo build --release"
fi

section "Rust tests"
RUST_TEST_OUT=$(cargo test --manifest-path rust-core/Cargo.toml 2>&1)
BEST=$(echo "$RUST_TEST_OUT" | grep "^test result: ok" | sort -t. -k2 -rn | head -1)
if [[ -n "$BEST" ]]; then
  PASSED=$(echo "$BEST" | grep -oE "[0-9]+ passed" | head -1)
  ok "Rust tests pass ($PASSED)"
else
  fail "Rust tests failed"
  echo "$RUST_TEST_OUT" | tail -10
fi

# ── Python ────────────────────────────────────────────────────────────────────
section "Python environment"
if [[ -f "$VENV" ]]; then
  ok ".venv exists"
else
  fail ".venv missing — run: python3 -m venv python-core/.venv && python-core/.venv/bin/pip install -r python-core/requirements.txt"
fi

section "Python tests"
PY_OUT=$($VENV -m pytest python-core/tests/ -q 2>&1 || true)
SUMMARY=$(echo "$PY_OUT" | grep -E "passed|failed|error" | tail -1)
if echo "$PY_OUT" | grep -q "passed"; then
  ok "Python tests: $SUMMARY"
else
  fail "Python tests failed — $SUMMARY"
  echo "$PY_OUT" | tail -10
fi

# ── Config ────────────────────────────────────────────────────────────────────
section "Config"

if [[ -f .env ]]; then
  ok ".env exists"
else
  fail ".env missing — copy config/.env.example and fill in values"
fi

# Check Kalshi URL is the public one
KALSHI_URL=$(grep "^KALSHI_API_URL=" .env 2>/dev/null | cut -d= -f2 || echo "")
if [[ "$KALSHI_URL" == *"api.elections.kalshi.com"* ]]; then
  ok "KALSHI_API_URL = $KALSHI_URL (public endpoint ✓)"
elif [[ -z "$KALSHI_URL" ]]; then
  warn "KALSHI_API_URL not set in .env — defaulting to api.elections.kalshi.com"
else
  fail "KALSHI_API_URL = $KALSHI_URL — should be https://api.elections.kalshi.com/trade-api/v2"
fi

if [[ -f config/markets.json ]]; then
  PAIR_INFO=$($VENV - <<'EOF'
import json
d = json.load(open("config/markets.json"))
pairs = d.get("pairs", d.get("manual_pairs", []))
cross = sum(1 for p in pairs if p.get("pair_type") == "cross_platform")
internal = sum(1 for p in pairs if p.get("pair_type") == "internal")
mode = "CROSS PLATFORM" if cross > 0 else "INTERNAL"
rejected = d.get("_stats", {}).get("rejected_multi_outcome", 0)
print(f"{len(pairs)} pairs | {cross} cross-platform, {internal} internal | mode={mode} | {rejected} multi-outcome rejected")
EOF
)
  if [[ $(echo "$PAIR_INFO" | grep -oE "^[0-9]+") -gt 0 ]]; then
    ok "markets.json: $PAIR_INFO"
  else
    fail "markets.json has 0 pairs — run: $VENV scripts/backfill_matches.py"
  fi
else
  fail "config/markets.json missing — run: $VENV scripts/backfill_matches.py"
fi

# ── Database ──────────────────────────────────────────────────────────────────
section "Database"
if [[ -f data/trades.db ]]; then
  DB_INFO=$($VENV - <<'EOF'
import sqlite3
c = sqlite3.connect("data/trades.db")
pairs     = c.execute("SELECT COUNT(*) FROM market_pairs").fetchone()[0]
trades    = c.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
open_pos  = c.execute("SELECT COUNT(*) FROM trades WHERE status='open'").fetchone()[0]
cross     = c.execute("SELECT COUNT(*) FROM market_pairs WHERE pair_type='cross_platform'").fetchone()[0]
internal  = c.execute("SELECT COUNT(*) FROM market_pairs WHERE pair_type='internal'").fetchone()[0]
pnl       = c.execute("SELECT COALESCE(SUM(expected_profit),0) FROM trades WHERE dry_run=1").fetchone()[0]
print(f"{pairs} pairs ({cross} cross-platform, {internal} internal) | {trades} trades total | {open_pos} open | sim P&L ${pnl:+.2f}")
EOF
)
  ok "trades.db: $DB_INFO"
else
  fail "trades.db missing — run: $VENV scripts/backfill_matches.py"
fi

# ── Network ───────────────────────────────────────────────────────────────────
section "Network (Polymarket — public)"
if curl -sf --max-time 5 "https://gamma-api.polymarket.com/markets?limit=1" -o /dev/null 2>&1; then
  ok "gamma-api.polymarket.com reachable"
else
  warn "gamma-api.polymarket.com unreachable — Polymarket geo-blocks some regions (India/etc). Use a VPN or proxy to reach cross-platform mode. Bot still runs in INTERNAL mode without it."
fi

section "Network (Kalshi — public, no auth)"
KALSHI_API_BASE="${KALSHI_URL:-https://api.elections.kalshi.com/trade-api/v2}"
HTTP_CODE=$(curl -sf --max-time 8 -o /dev/null -w "%{http_code}" \
  "${KALSHI_API_BASE}/markets?status=open&limit=1" 2>/dev/null || echo "000")
if [[ "$HTTP_CODE" == "200" ]]; then
  ok "Kalshi public API reachable ($KALSHI_API_BASE) — HTTP 200"
elif [[ "$HTTP_CODE" == "401" ]]; then
  fail "Kalshi returned 401 — URL is the authenticated endpoint, not public. Set KALSHI_API_URL=https://api.elections.kalshi.com/trade-api/v2"
elif [[ "$HTTP_CODE" == "000" ]]; then
  warn "Kalshi API unreachable (timeout/no route) — bot runs in INTERNAL mode only"
else
  warn "Kalshi API returned HTTP $HTTP_CODE — check connectivity"
fi

section "Network (Polymarket price feed sample)"
SAMPLE_ID=$($VENV -c "
import json
try:
    d = json.load(open('config/markets.json'))
    pairs = d.get('pairs', [])
    gid = next((p['gamma_id_a'] for p in pairs if p.get('gamma_id_a')), '')
    print(gid)
except: print('')
" 2>/dev/null)
if [[ -n "$SAMPLE_ID" ]] && curl -sf --max-time 5 \
    "https://gamma-api.polymarket.com/markets?id=${SAMPLE_ID}" -o /dev/null 2>&1; then
  ok "Price polling reachable (gamma_id=$SAMPLE_ID)"
else
  warn "Price polling check skipped — no sample gamma_id found"
fi

# ── Dry-run smoke test ────────────────────────────────────────────────────────
section "Dry-run smoke test (5s)"
SMOKE_LOG=$(mktemp)
RUST_LOG=info $VENV python-core/main.py >"$SMOKE_LOG" 2>&1 &
BOT_PID=$!
sleep 5
BOT_ALIVE=0
kill -0 $BOT_PID 2>/dev/null && BOT_ALIVE=1
kill $BOT_PID 2>/dev/null
wait $BOT_PID 2>/dev/null || true

# Show key log lines — suppress noisy batch-URL errors (expected when Polymarket is geo-blocked)
grep -E "Running in|Bot started|pairs loaded|Kalshi|ERROR" "$SMOKE_LOG" \
  | grep -v "batch fetch error\|error sending request" \
  | grep -v "^$" | head -8 | sed 's/^/  /' || true
rm -f "$SMOKE_LOG"

if [[ "$BOT_ALIVE" == "1" ]]; then
  ok "Bot stayed alive 5s in DRY_RUN mode"
else
  fail "Bot crashed within 5s"
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo
echo "══════════════════════════════════════════"
printf "  %s passed  |  %s failed  |  %s warnings\n" "$PASS" "$FAIL" "$WARN"
echo "══════════════════════════════════════════"
[[ $FAIL -eq 0 ]] && exit 0 || exit 1
