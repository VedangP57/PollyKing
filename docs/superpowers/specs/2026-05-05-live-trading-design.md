# PolyyKing Live Trading — Design Spec
**Date:** 2026-05-05  
**Status:** Approved  
**Goal:** Bring PolyyKing from ~15% to ~80% live-trading readiness

---

## Problem Statement

The bot correctly detects real arbitrage gaps (price feed works, gap math is correct, 7-layer validator is solid). But the execution layer is a stub — Polymarket orders require EIP-712 wallet signatures that the current Rust implementation does not produce, Kalshi HMAC signing exists but is never wired in, there are zero cross-platform (Poly vs Kalshi) pairs, and the fee rate assumption is wrong for the markets being traded.

Flipping `DRY_RUN=false` today would result in 100% order rejection with no money at risk but also zero profit.

---

## Scope

### In scope
- Move all order execution from Rust to Python
- Implement Polymarket execution via `py_clob_client` SDK
- Implement Kalshi execution via Python REST + HMAC signing
- Cross-platform pair generation with daily refresh
- Cross-platform gaps as primary, internal gaps as fallback
- Per-market fee rate correction
- Reconciler fix (real resolution data instead of hardcoded 8%)
- Partial fill emergency close safety net
- Emergency positions table for manual recovery

### Out of scope
- Per-second latency optimization (not needed for this strategy)
- Automatic pair confidence learning
- Web-based monitoring dashboard
- Multi-account / multi-wallet support
- VPS deployment

---

## Architecture

### Current (broken execution)
```
Rust (price feed + gap detect + execute orders)
  ↕ stdin/stdout pipe (bidirectional)
Python (validate + log)
```

### New (clean separation)
```
Rust (price feed + gap detect ONLY)
  → stdout pipe (one-directional, gaps only)
Python (validate + execute + log)
  ├── PolymarketExecutor  → py_clob_client SDK
  └── KalshiExecutor      → REST + HMAC (Python)
```

**Key change:** The bridge becomes one-directional. Rust writes gap events to stdout. Python only reads. Python never writes back to Rust stdin. The Rust `executor.rs` `live_execute()` function is deleted. `executor.py` in Python is replaced with two new executor classes.

---

## Components

### 1. `PolymarketExecutor` (`python-core/polymarket_executor.py`)

Wraps `py_clob_client`. Handles EIP-712 signing internally via the SDK.

**Interface:**
```python
class PolymarketExecutor:
    def __init__(self, config: dict): ...
    async def place_order(self, token_id: str, side: str, amount_usdc: float) -> dict:
        # Returns: {"order_id": str, "status": str, "amount_filled": float}
        # Raises: ExecutorError on failure
```

**Dependencies:** `py_clob_client`, `POLYMARKET_API_KEY`, `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_WALLET_ADDRESS`

**Notes:**
- Client is instantiated once at startup (not per trade) — handles nonce caching internally
- `side` is `"BUY"` — we always buy the cheap side
- `token_id` comes directly from `gap["polymarket_token"]` (already the correct hex format)

---

### 2. `KalshiExecutor` (`python-core/kalshi_executor.py`)

Direct REST with Python HMAC-SHA256 signing. Ports the existing `sign_request()` logic from Rust.

**Interface:**
```python
class KalshiExecutor:
    def __init__(self, config: dict): ...
    async def place_order(self, ticker: str, action: str, count: int) -> dict:
        # Returns: {"order_id": str, "status": str}
        # Raises: ExecutorError on failure
```

**Signing logic:**
```python
def _sign(self, method: str, path: str) -> dict:
    timestamp = str(int(time.time() * 1000))
    message = timestamp + method + path
    sig = hmac.new(self.api_secret.encode(), message.encode(), hashlib.sha256)
    signature = base64.b64encode(sig.digest()).decode()
    return {
        "Authorization": f"Token {self.api_key}",
        "Kalshi-Access-Key": self.api_key,
        "Kalshi-Access-Signature": signature,
        "Kalshi-Access-Timestamp": timestamp,
    }
```

**Key fix:** `count` = integer number of contracts (Kalshi contracts are $0.01 each). Convert: `count = int(amount_usdc * 100)`.

**Dependencies:** `KALSHI_API_KEY`, `KALSHI_API_SECRET`, `KALSHI_API_URL`

---

### 3. `TwoLegExecutor` (`python-core/two_leg_executor.py`)

Orchestrates concurrent placement of both legs. Handles partial fill emergency close.

**Interface:**
```python
class TwoLegExecutor:
    async def execute(self, gap: dict, bet_size: float) -> dict | None:
        # Returns trade confirmation dict or None on full failure
```

**Logic:**
```python
async def execute(self, gap, bet_size):
    leg_a, leg_b = build_legs(gap, bet_size)
    
    result_a, result_b = await asyncio.gather(
        self._place_leg(leg_a), self._place_leg(leg_b),
        return_exceptions=True
    )
    
    a_ok = not isinstance(result_a, Exception)
    b_ok = not isinstance(result_b, Exception)
    
    if a_ok and b_ok:
        return build_confirmation(result_a, result_b, bet_size)
    
    if not a_ok and not b_ok:
        log.warning("Both legs failed — no position opened")
        return None
    
    # Partial fill: emergency close the filled leg
    filled = result_a if a_ok else result_b
    await self._emergency_close(filled, gap)
    log_emergency_position(gap, filled)
    return None
```

**Emergency positions table** (new DB table):
```sql
CREATE TABLE IF NOT EXISTS emergency_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT,
    platform TEXT,
    order_id TEXT,
    side TEXT,
    amount_usdc REAL,
    opened_at TEXT,
    closed_at TEXT,
    status TEXT  -- 'open', 'closed_manually', 'closed_auto'
);
```

---

### 4. Backfill / Pair Generation (`scripts/backfill_matches.py`)

Queries both platforms and produces cross-platform pairs using the existing `Matcher` class.

**Trigger:** Runs at bot startup if `markets.json` mtime is >24 hours old.

**Flow:**
```
1. GET https://gamma-api.polymarket.com/markets?active=true&limit=500  (paginated)
2. GET https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=200 (paginated)
3. Run Matcher.match(poly_markets, kalshi_markets) → cross-platform pairs
4. For each pair: fetch feeSchedule.rate from Gamma API response → store as fee_rate
5. Merge with existing internal pairs in markets.json (don't overwrite, append)
6. Write updated markets.json
7. Print summary: N cross-platform pairs added, M internal pairs retained
```

**Output format per cross-platform pair:**
```json
{
  "pair_type": "cross_platform",
  "token_a": "<polymarket YES token hex>",
  "token_b": "<kalshi ticker>",
  "market_id": "<polymarket_slug>",
  "confidence": "high|medium",
  "match_method": "exact|fuzzy|manual",
  "gamma_id_a": "<gamma market id>",
  "fee_rate": 0.04,
  "polymarket_title": "...",
  "kalshi_title": "..."
}
```

---

### 5. Comparator Priority Logic

**Cross-platform first:** The comparator already runs both pair types in parallel. Priority is enforced via thresholds:

| Pair type | Min gap | EV min (after fee) |
|-----------|---------|-------------------|
| cross_platform | 5¢ | 1.0¢ |
| internal | 8¢ | 2.0¢ |

Internal pairs require a higher gap to clear because negRisk order mechanics on Polymarket are more complex (two orders on the same platform, same nonce context). This naturally routes the bot toward cross-platform gaps first while retaining internal as a fallback when cross-platform gaps are absent.

Both thresholds are configurable via `.env`:
```
CROSS_PLATFORM_MIN_GAP_CENTS=5
INTERNAL_MIN_GAP_CENTS=8
```

---

### 6. Fee Rate Fix

**Current:** Global `EV_TAKER_FEE_RATE=0.02` applied to all markets.

**New:** Per-pair `fee_rate` stored in `markets.json` at backfill time, passed through to `GapDetector` and `ev_engine.py` as `gap["fee_rate"]`. Falls back to `0.04` (conservative) if not set.

Confirmed fee rates from Gamma API:
- `"feeType":"politics_fees"` → `rate: 0.04` (4%)
- `"feeType":"standard"` → `rate: 0.02` (2%)

**Impact:** Several currently-approved gaps near the 5¢ minimum become marginal or rejected at 4%. The EV gate will correctly filter them.

---

### 7. Reconciler Fix (`python-core/reconciler.py`)

**Current (broken):**
```python
actual_profit = amount_usdc * 0.08  # hardcoded placeholder
```

**New:**
```python
def compute_actual_profit(resolution: str, poly_side: str, kalshi_side: str,
                          amount_usdc: float, fee_rate: float) -> float:
    # For guaranteed arb: one leg wins $k, other leg loses its stake
    # k = amount_usdc / combined (recovered from stored gap_cents)
    stake_per_leg = amount_usdc / 2
    combined = 1.0 - gap_cents / 100.0
    k = amount_usdc / combined  # payout from winning leg
    
    # Determine which leg wins
    poly_wins = (resolution == "YES" and poly_side == "YES") or \
                (resolution == "NO" and poly_side == "NO")
    
    gross = k - amount_usdc  # guaranteed regardless of which leg wins
    fee = fee_rate * amount_usdc
    return round(gross - fee, 4)
```

Note: `gap_cents` needs to be stored in the `trades` table for reconciliation. Add `gap_cents REAL` column to trades schema (migration on startup).

---

## Data Flow

### Startup
```
main.py starts
  → check markets.json age
    → if >24h: run backfill_matches.py (~15s)
  → load pairs into memory (cross-platform + internal)
  → init TwoLegExecutor (PolymarketExecutor + KalshiExecutor)
  → start Rust subprocess (price feed only)
  → start reconciler background task (every 5 min)
  → start gap reader loop
```

### Per-gap execution
```
Rust stdout → gap event (JSON)
  → Python reads line
  → GapDetector.validate(gap)  [uses gap["fee_rate"]]
    → fail: log rejection, continue
    → pass: continue
  → TwoLegExecutor.execute(gap, kelly_bet_size)
    → place both legs concurrently
    → both succeed: log_trade(db, trade)
    → both fail: log warning, continue
    → one succeeds: emergency_close + log_emergency_position
  → loop
```

### Reconciliation (background, every 5 min)
```
get_open_live_trades(db)
  → for each trade:
    → fetch Gamma API for resolution status
    → if resolved: compute_actual_profit(...)
    → resolve_trade(db, trade_id, actual_profit, status)
```

---

## Error Handling

| Scenario | Handling |
|----------|----------|
| Both legs fail | Log as `failed`, no position, no risk |
| One leg fills, one fails | `emergency_close(filled_leg)` immediately, write to `emergency_positions` |
| `emergency_close` also fails | Log `ERROR`, write to `emergency_positions` with status `open` — requires manual close |
| API 429 rate limit | Exponential backoff (1s, 2s, 4s), skip gap after 3 retries |
| Order timeout >10s | Cancel order, treat as failure |
| Rust subprocess dies | Restart with 5s delay, log `ERROR` |
| Invalid API key | Hard stop — `sys.exit(1)` with clear error message |
| Network error | Skip trade, log warning, next gap |

---

## Files Changed

| File | Change |
|------|--------|
| `python-core/polymarket_executor.py` | **New** — py_clob_client wrapper |
| `python-core/kalshi_executor.py` | **New** — REST + HMAC signing |
| `python-core/two_leg_executor.py` | **New** — concurrent execution + partial fill handler |
| `python-core/executor.py` | **Delete** — replaced by above three. `_compute_bet_size` moves into `TwoLegExecutor` |
| `python-core/main.py` | Update — use TwoLegExecutor, startup backfill check, remove Rust stdin writes |
| `python-core/reconciler.py` | Update — real profit calculation, add gap_cents lookup |
| `python-core/detector.py` | Update — use gap["fee_rate"] instead of global config |
| `python-core/ev_engine.py` | Update — accept fee_rate as parameter |
| `python-core/tracker.py` | Update — add gap_cents column to trades, emergency_positions table |
| `scripts/backfill_matches.py` | Update — fetch both APIs, merge pairs, store fee_rate |
| `rust-core/src/executor.rs` | Update — delete live_execute(), keep dry_run_execute() for testing |
| `rust-core/src/bridge.rs` | Update — remove stdin reader (one-directional only) |
| `config/.env.example` | Update — add CROSS_PLATFORM_MIN_GAP_CENTS, INTERNAL_MIN_GAP_CENTS |

---

## Dependencies to Add

```
# python-core/pyproject.toml
py-clob-client>=0.19.0   # Polymarket official SDK
```

Kalshi requires no new dependency — standard library `hmac`, `hashlib`, `base64` + existing `aiohttp`.

---

## Testing Plan

1. Unit test `KalshiExecutor._sign()` — verify HMAC output matches known test vector
2. Unit test `TwoLegExecutor` — mock both legs, test all 4 outcome paths (both ok, both fail, a fails, b fails)
3. Unit test `compute_actual_profit()` — verify YES resolution and NO resolution both compute correctly
4. Unit test `backfill_matches.py` — mock Gamma + Kalshi API responses, verify pair output
5. Integration test (dry-run): run full bot against live APIs with `DRY_RUN=true`, verify cross-platform gaps appear within 60s
6. Live smoke test: one $10 trade on a high-confidence cross-platform pair with known gap

---

## Readiness After This Work

| Layer | Before | After |
|-------|--------|-------|
| Price feed | ✅ Working | ✅ Unchanged |
| Gap detection | ✅ Working | ✅ Unchanged |
| Cross-platform pairs | ❌ 0 pairs | ✅ ~50-200 pairs from backfill |
| Polymarket execution | ❌ Wrong auth | ✅ py_clob_client |
| Kalshi execution | ❌ No signing | ✅ HMAC signed |
| Fee accuracy | ❌ 2% global | ✅ Per-market from API |
| Partial fill safety | ❌ Missing | ✅ Emergency close |
| Reconciler | ❌ Hardcoded 8% | ✅ Real resolution data |
| **Overall** | **~15%** | **~80%** |

The remaining 20% gap to 100% is: production monitoring/alerting, automatic confidence learning, full fill confirmation (currently assumes market orders fill immediately), and battle-testing across different market conditions.
