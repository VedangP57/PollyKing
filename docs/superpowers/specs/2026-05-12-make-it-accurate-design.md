# Spec: Make It Accurate
**Date:** 2026-05-12
**Target score:** 8.0 → 9.0/10
**Estimated time:** ~4 hours
**Depends on:** Make It Safe spec fully implemented

## Goal

Improve edge capture and ensure the bot never executes a trade where fees exceed the gap. After this spec, fee calculations use real per-market rates, position sizing respects book depth, unstable markets are circuit-broken, and a single bug can't drain the account overnight.

---

## Items

### 9. Startup fee cache (`polymarket_executor.py` + `detector.py`)

**Problem:** `detector.py` uses `gap.get("fee_rate", ev_taker_fee_rate=0.02)` — a flat default that does not reflect Polymarket's per-category taker fees (0–1.8%, enabled March 2026). `markets.json` has `fee_rate=0.04` but this appears to be a combined estimate, not the actual CLOB taker fee.

**Design:**

New method on `PolymarketExecutor`:
```python
async def warm_fee_cache(self, token_ids: list[str]) -> None:
    """Fetch taker fee rate for each token from CLOB API. Cached in self._fee_cache."""
```

- Calls `GET /markets?token_id=<id>` (or batch equivalent) for each token in `markets.json`.
- Stores `{token_id: taker_fee_rate}` in `self._fee_cache`.
- Falls back to `0.02` if token not returned by API.

Called from `main.py` after pairs load, before Rust process spawns:
```python
await executor._poly.warm_fee_cache([p["token_a"] for p in pairs])
```

`detector.py` receives the cache via config or direct reference:
```python
fee_cache = config.get("_fee_cache", {})
taker_fee_rate = fee_cache.get(gap.get("polymarket_token", ""), 0.02)
```

**Config:** Add `FEE_CACHE_REFRESH_INTERVAL_S=3600` to `.env.example`. Background task refreshes cache hourly.

---

### 10. Depth-constrained position sizing (`two_leg_executor.py:_compute_bet_size`)

**Problem:** Kelly sizing ignores whether the order book can absorb the position. Buying $100 on a book with $120 visible depth means 83% market impact — the bot moves the price against itself.

**Design:** One line added to `_compute_bet_size` after Kelly calculation:

```python
max_depth_fraction = self._config.get("max_depth_fraction", 0.25)
poly_liq = gap.get("poly_liquidity_usdc", float("inf"))
kalshi_liq = gap.get("kalshi_liquidity_usdc", float("inf"))
depth_cap = min(poly_liq, kalshi_liq) * max_depth_fraction
bet_size = min(bet_size, depth_cap)
bet_size = max(bet_size, min_bet)  # never go below minimum
```

`poly_liquidity_usdc` and `kalshi_liquidity_usdc` are already in every `Gap` emitted by Rust.

**Config:** Add `MAX_DEPTH_FRACTION=0.25` to `.env.example`.

---

### 11. Per-market circuit breaker (`python-core/circuit_breaker.py`)

**Problem:** No mechanism to pause a specific market after repeated fill failures. A malfunctioning market (thin book, erratic pricing) can trigger repeated failed execution attempts.

**Design:** New `CircuitBreaker` class:

```python
class CircuitBreaker:
    def __init__(self, max_failures: int = 3, window_s: float = 600.0):
        ...
    def record_failure(self, market_id: str) -> None: ...
    def is_open(self, market_id: str) -> bool: ...
    def reset(self, market_id: str) -> None: ...
```

- In-memory `dict[str, deque[float]]` of failure timestamps per market.
- `is_open`: returns True if `len([t for t in timestamps if now - t < window_s]) >= max_failures`.
- Auto-expires: failures older than `window_s` are evicted on each check.
- `reset`: called on successful fill — clears that market's breaker.

Called in `_handle_gap_inner` (before `detector.validate()`):
```python
if circuit_breaker.is_open(market_id):
    return  # silently skip — logged at record_failure time
```

Called in executor on failed execution:
```python
if confirmation is None:
    circuit_breaker.record_failure(market_id)
```

**Config:** Add `CIRCUIT_BREAKER_MAX_FAILURES=3`, `CIRCUIT_BREAKER_WINDOW_S=600` to `.env.example`.

---

### 12. Chaos engineering tests (`python-core/tests/test_chaos.py`)

**Problem:** No tests verify correct behavior after crash mid-trade, DB corruption, or bridge death. Recovery behavior is untested.

**Design:** Three subprocess-based test scenarios:

**Scenario A — crash between leg placement and fill:**
1. Start bot in dry_run mode with a mock that hangs after leg_a completes.
2. `SIGKILL` the process.
3. Restart bot.
4. Assert `emergency_positions` table has entry for leg_a's order_id (startup_audit detected it).

**Scenario B — DB corruption at startup:**
1. Write garbage bytes into first 100 bytes of `trades.db`.
2. Run startup_check.
3. Assert `sys.exit(1)` fires with message containing "integrity_check".

**Scenario C — Rust bridge death:**
1. Start bot normally (dry_run).
2. Kill Rust subprocess by PID after 2s.
3. Assert Python logs `[rust] process exited` and Python process also exits within 5s (no hang).

**Implementation note:** Tests run against a temp DB copy. They spawn subprocesses and use `subprocess.Popen` with timeout. Marked `@pytest.mark.chaos` so they can be excluded from fast test runs.

---

### 13. Daily loss auto-shutdown (`detector.py:165`)

**Problem:** When daily loss limit is hit, `validate()` returns `(False, "Daily loss limit hit")`. The bot continues running, continues receiving gaps, continues calling `validate()` every gap — it just always rejects. One configuration bug (wrong `max_daily_loss_usdc`) could still allow trades if the check is bypassed.

**Design:** Add a second path when the limit is reached:

```python
if daily_loss >= max_loss:
    log.critical(
        "DAILY LOSS LIMIT REACHED: $%.2f >= $%.2f — writing kill switch and shutting down",
        daily_loss, max_loss,
    )
    # Persist kill switch so restart also refuses to trade
    db_conn.execute(
        "INSERT OR REPLACE INTO bot_state (key, value) VALUES ('kill_switch', '1')",
    )
    db_conn.commit()
    raise SystemExit(1)
```

The existing kill switch check in `RiskEngine.check_kill_switches()` (which reads `bot_state.kill_switch`) then blocks re-entry on restart until manually cleared:
```bash
sqlite3 data/trades.db "DELETE FROM bot_state WHERE key='kill_switch';"
```

**This is the only valid way to resume after a daily loss shutdown** — documented in the runbook (Spec 3).

---

### 14. Resolution rule mismatch detection (`scripts/backfill_matches.py`)

**Problem:** Polymarket and Kalshi may resolve a "same" market using different oracles, cutoff timestamps, or timezone conventions. A pair where Kalshi resolves at midnight ET and Polymarket at midnight UTC creates a 5-hour window of naked directional risk.

**Design:** Added to the pair-creation step in `backfill_matches.py`. For each candidate cross-platform pair:

1. Fetch Kalshi market's `close_time` (UTC).
2. Fetch Polymarket market's `end_date_iso` (UTC).
3. Compute `delta_hours = abs(kalshi_close - poly_end) / 3600`.
4. If `delta_hours > 6`: flag pair as `resolution_mismatch=True`, log WARNING, do not add to active pairs.
5. If `delta_hours > 0 and <= 6`: add pair but set `confidence="low"` (detector rejects low-confidence pairs).

**Output:** Mismatch pairs are written to `data/resolution_mismatches.json` for manual review. Summary count logged at backfill completion.

**Config:** Add `MAX_RESOLUTION_DELTA_HOURS=6` to `.env.example`.

---

## Data Flow Changes

```
main.py startup:
  startup_check.run_all(config)          # Spec 1
  executor._poly.warm_fee_cache(tokens)  # NEW: populate fee cache
  pairs loaded
  Rust spawns
    ↓
_handle_gap_inner:
  circuit_breaker.is_open(market_id)?    # NEW: skip if open
    → return
  detector.validate(gap)
    Check 1 EV: uses fee_cache           # CHANGED: real taker fee
    Check 1c: edge/spread ratio          # Spec 1
    ...
  _compute_bet_size: depth cap applied   # NEW
  executor.execute(gap)
    FOK→GTC                              # Spec 1
  confirmation=None → circuit_breaker.record_failure()  # NEW
  daily_loss >= max → SystemExit(1)      # CHANGED
```

---

## Error Handling

- Fee cache miss: falls back to `0.02` — safe conservative default.
- Fee cache API error at startup: log WARNING, proceed with defaults (non-fatal).
- Circuit breaker: silent skip, failure logged at `record_failure` time.
- Daily loss shutdown: `SystemExit(1)` — clean exit, kill switch persisted to DB.
- Resolution mismatch: pairs excluded from active set, written to review file.

---

## Testing

| Test | Location | What it verifies |
|------|----------|-----------------|
| `test_fee_cache_populates` | `test_polymarket_executor.py` | `warm_fee_cache` stores rates per token |
| `test_fee_cache_miss_defaults` | `test_detector.py` | Missing token falls back to 0.02 |
| `test_depth_cap_limits_bet` | `test_two_leg_executor.py` | Bet capped at 25% of min(poly_liq, kalshi_liq) |
| `test_circuit_breaker_opens` | `test_circuit_breaker.py` | 3 failures in window → is_open=True |
| `test_circuit_breaker_expires` | `test_circuit_breaker.py` | Failure older than window → evicted |
| `test_circuit_breaker_resets` | `test_circuit_breaker.py` | Successful fill clears breaker |
| `test_chaos_*` (3 scenarios) | `test_chaos.py` | Crash/DB/bridge recovery |
| `test_daily_loss_shutdown` | `test_detector.py` | SystemExit(1) + kill switch written |
| `test_resolution_mismatch_excluded` | `test_backfill.py` | Delta > 6h → pair not added |
