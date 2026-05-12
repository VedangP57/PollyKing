# Spec: Make It Scale
**Date:** 2026-05-12
**Target score:** 9.0 → 9.5/10
**Estimated time:** ~3 hours
**Depends on:** Make It Safe + Make It Accurate both fully implemented

## Goal

Engineering excellence that matters at $1k+ bankroll or when running unattended for weeks. None of these items cause losses at current capital scale — they become important as the system scales.

---

## Items

### 15. Prometheus metrics expansion (`python-core/metrics.py`)

**Problem:** Prometheus is already running on `:9090` but only tracks basic counters. No visibility into WebSocket health, fill rates, or current P&L.

**Design:** Add three new gauges to `metrics.py`:

```python
# WebSocket health — updated by zombie watchdog (Spec 1) via IPC or shared state
ws_staleness_seconds = Gauge(
    "arb_ws_staleness_seconds",
    "Seconds since last real price event from Polymarket WS",
)

# Fill quality — updated on each fill poll result
fill_success_rate = Gauge(
    "arb_fill_success_rate",
    "Fraction of fill polls that returned 'filled' in rolling 1h window",
)

# Daily P&L — polled from DB every 60s by background task
daily_pnl_usdc = Gauge(
    "arb_daily_pnl_usdc",
    "Net P&L for current calendar day (UTC) in USDC",
)
```

Background task in `main.py`:
```python
async def _update_pnl_metric():
    while True:
        await asyncio.sleep(60)
        pnl = tracker.get_daily_pnl(db_conn)
        _metrics.daily_pnl_usdc.set(pnl)
```

Rust-side: zombie watchdog (Spec 1) writes `ws_stale_seconds` to stdout as a JSON event `{"event": "ws_staleness", "seconds": 42}`. Python `_read_stdout` handler updates the gauge.

**Local scrape config** added at `config/prometheus.yml`:
```yaml
global:
  scrape_interval: 15s
scrape_configs:
  - job_name: polyking
    static_configs:
      - targets: ["localhost:9090"]
```

Run with: `prometheus --config.file=config/prometheus.yml`

---

### 16. NLP-based cross-platform matching (`scripts/backfill_matches.py`)

**Problem:** Current matching uses exact/fuzzy title matching that fails on differently-phrased markets. False positives (wrong pairs) create resolution risk. False negatives (missed pairs) reduce opportunity.

**Design:** Replace current title matching with a normalized similarity pipeline:

**Step 1 — Normalize both titles:**
```python
def normalize(title: str) -> str:
    t = title.lower().strip()
    # Expand common abbreviations
    t = t.replace("u.s.", "us").replace("u.k.", "uk").replace("pct", "percent")
    # Strip punctuation except hyphens and percent signs
    t = re.sub(r"[^\w\s\-%]", "", t)
    # Collapse whitespace
    return re.sub(r"\s+", " ", t).strip()
```

**Step 2 — Compute similarity:**
```python
from difflib import SequenceMatcher

def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()
```

**Step 3 — Apply gates:**
- Similarity ≥ 0.80 → candidate pair
- Additional checks (all must pass):
  - Year extracted from both titles must match (or both absent)
  - Market type matches: binary vs binary (both have exactly 2 outcomes)
  - If asset mentioned (BTC, ETH, USD), must match on both sides

**Step 4 — Score and rank:**
- Pairs scored by similarity. Top match per Polymarket token selected.
- Similarity 0.80–0.90 → `confidence="low"`
- Similarity > 0.90 → `confidence="medium"`
- Similarity > 0.95 + asset + year match → `confidence="high"`

**Dependency:** `difflib` is stdlib. No new packages required.

---

### 17. Automated pair invalidation (`scripts/backfill_matches.py` + scheduled task)

**Problem:** Expired or resolved markets stay in `markets.json`. Dead pairs waste WebSocket subscription slots and generate ghost gaps that fail at execution.

**Design:**

New function `invalidate_expired_pairs(pairs: list) -> list`:
```python
def invalidate_expired_pairs(pairs: list) -> list:
    """Remove pairs where either market has resolved or expired on either platform."""
    active = []
    for pair in pairs:
        poly_active = _check_polymarket_active(pair["token_a"])
        kalshi_active = _check_kalshi_active(pair["token_b"])
        if poly_active and kalshi_active:
            active.append(pair)
        else:
            log.info("Invalidating expired pair: %s (poly=%s, kalshi=%s)",
                     pair["market_id"], poly_active, kalshi_active)
    return active
```

- `_check_polymarket_active`: calls `GET /markets?token_id=<id>`, checks `active=true` and `end_date > now`.
- `_check_kalshi_active`: calls Kalshi market endpoint, checks `status=open`.

Called at the end of `backfill_matches.py` — pairs are filtered before writing `markets.json`.

**Scheduled run:** Add to cron or launchd as a daily job:
```bash
cd /path/to/PolyyKing && uv run python scripts/backfill_matches.py --invalidate-only
```

`--invalidate-only` flag: skip the matching step, only run invalidation on existing `markets.json`.

---

### 18. Idempotency keys on trade attempts (`tracker.py` + `two_leg_executor.py`)

**Problem:** If the bot crashes after order placement but before `log_trade()` completes, `startup_audit.py` may detect an orphan position but has no record of the attempted trade. Restarting can't distinguish "this orphan was already being handled" from "this is a new unknown position".

**Design:**

New table `trade_attempts` in `tracker.py`:
```sql
CREATE TABLE IF NOT EXISTS trade_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL UNIQUE,
    market_id TEXT NOT NULL,
    pair_type TEXT NOT NULL,
    gap_cents REAL,
    bet_usdc REAL,
    attempted_at TEXT NOT NULL,
    confirmed INTEGER DEFAULT 0,
    trade_id INTEGER REFERENCES trades(id)
);
CREATE INDEX IF NOT EXISTS idx_attempts_confirmed ON trade_attempts(confirmed, attempted_at DESC);
```

In `two_leg_executor.execute()`, before order placement:
```python
import uuid
attempt_id = str(uuid.uuid4())
tracker.log_trade_attempt(db_conn, {
    "attempt_id": attempt_id,
    "market_id": gap["market_id"],
    "pair_type": gap.get("pair_type", "cross_platform"),
    "gap_cents": gap.get("gap_cents"),
    "bet_usdc": bet_size,
})
```

After confirmed execution, mark as confirmed:
```python
tracker.confirm_trade_attempt(db_conn, attempt_id, trade_id)
```

`startup_audit.py` queries for unconfirmed attempts:
```python
unconfirmed = db_conn.execute(
    "SELECT * FROM trade_attempts WHERE confirmed=0 AND attempted_at > datetime('now', '-1 hour')"
).fetchall()
```
Each unconfirmed attempt → logged as WARNING with `attempt_id` and `market_id` for manual cross-reference.

---

### 19. Operational runbook (`docs/runbook.md`)

**Problem:** No documented procedures for emergency close, reconciliation, kill switch reset, or pair refresh.

**Design:** Single markdown document. Four sections:

**Section 1 — Normal operation**
- How to start the bot (dry-run vs live)
- How to check current status (`sqlite3 data/trades.db` queries)
- How to read Prometheus metrics

**Section 2 — Emergency close**
- How to manually cancel a Polymarket order via CLOB API (`curl` command)
- How to manually close a Kalshi position via API
- How to mark an emergency_position as closed in the DB

**Section 3 — Kill switch**
- How the kill switch gets set (daily loss limit, manual)
- How to verify it's set: `sqlite3 data/trades.db "SELECT * FROM bot_state WHERE key='kill_switch';"`
- How to clear it: `sqlite3 data/trades.db "DELETE FROM bot_state WHERE key='kill_switch';"`
- Required checks before clearing: confirm P&L, confirm no open positions

**Section 4 — Pair refresh**
- When to run `scripts/backfill_matches.py`
- How to add a manual pair to `markets.json`
- How to invalidate a bad pair
- Resolution mismatch review: `cat data/resolution_mismatches.json`

---

## Data Flow Changes

```
startup: fee cache warmed (Spec 2)
         pairs loaded → invalidation check (NEW: dead pairs removed)
         Rust spawns
           ↓
gap detected
  circuit breaker check (Spec 2)
  detector.validate()
  _compute_bet_size() → depth cap (Spec 2)
  tracker.log_trade_attempt()    # NEW: idempotency record
  executor.execute()
    FOK→GTC (Spec 1)
  tracker.confirm_trade_attempt()  # NEW: mark confirmed
  metrics updated (daily_pnl, fill_rate)   # NEW
    ↓
background tasks:
  _update_pnl_metric() every 60s  # NEW
  opp_engine.evict_stale() every 60s (existing)
  reconciler.run_forever() every 5min (existing)
  fee_cache refresh every 1h (Spec 2)
  pair_invalidation daily (NEW: cron/launchd)
```

---

## Error Handling

- Fee refresh failure: log WARNING, keep cached values (non-fatal).
- Pair invalidation API error: log WARNING, keep existing pair (safe — better to keep a possibly-expired pair than silently drop a live one).
- Idempotency write failure: log WARNING, proceed with execution (non-fatal — orphan detection still catches crashes).
- NLP matching: similarity < 0.80 → pair rejected (no false positives; false negatives are recoverable).

---

## Testing

| Test | Location | What it verifies |
|------|----------|-----------------|
| `test_pnl_metric_updates` | `test_metrics.py` | Background task sets `daily_pnl_usdc` gauge |
| `test_ws_staleness_event_parsed` | `test_main.py` | `ws_staleness` JSON event updates gauge |
| `test_normalize_title` | `test_backfill.py` | Normalization handles abbreviations and punctuation |
| `test_nlp_match_high_confidence` | `test_backfill.py` | >0.95 similarity → high confidence |
| `test_nlp_match_rejects_low` | `test_backfill.py` | <0.80 similarity → not paired |
| `test_invalidation_removes_expired` | `test_backfill.py` | Resolved market removed from pairs |
| `test_trade_attempt_logged_before_order` | `test_two_leg_executor.py` | attempt_id in DB before place_order called |
| `test_unconfirmed_attempt_flagged` | `test_startup_audit.py` | Unconfirmed attempt → WARNING logged |
| `test_runbook_queries_work` | (manual) | All sqlite3 queries in runbook return valid results |
