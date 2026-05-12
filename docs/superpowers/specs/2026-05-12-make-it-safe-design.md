# Spec: Make It Safe
**Date:** 2026-05-12
**Target score:** 7.5 → 8.0/10
**Estimated time:** ~3 hours
**Depends on:** nothing (these are blockers)

## Goal

Eliminate every scenario where the bot loses money due to its own bugs. After this spec, all confirmed execution bugs are fixed, the WebSocket feed is zombie-proof, and startup refuses to run with a broken configuration.

---

## Items

### 1. Internal leg_b fill verification (`two_leg_executor.py:273`)

**Problem:** `_gather_legs` skips fill-poll for leg_b on internal pairs because the guard is `if b_ok and kalshi_count is not None`. Internal pairs pass `kalshi_count=None`, so leg_b (a Polymarket order) is never verified. If it rests unfilled, the bot logs success while holding a one-sided position.

**Fix:** Replace the single guard with platform-aware routing:
```python
if b_ok and not dry_run:
    b_id = result_b.get("order_id", "")
    if b_id:
        b_platform = "kalshi" if kalshi_count is not None else "polymarket"
        verify.append((b_platform, self._poll_and_cancel(b_platform, b_id)))
```

**Tests:** Update `test_fill_polls_run_concurrently` to assert both legs are polled for internal pairs.

---

### 2. EV combined formula (`detector.py:89`)

**Problem:** Rust now sends `polymarket_price` as the actual execution price (NO price for dir1, YES ask for dir2). Detector computes `combined = (1 - poly_price) + kalshi_price` — the old formula for when poly_price was always the YES price. This double-inverts dir1, making combined > 1 always → every dir1 cross-platform gap is silently rejected by the EV gate.

**Fix:**
```python
# detector.py line 86-89 — same formula for all pair types
# Rust sends actual execution prices for both legs
combined = poly_price + kalshi_price
```

**Side effects:** Also fix the comment above (remove the incorrect `(1-poly)` formula). Update three test assertions in `test_detector.py` that use YES prices for cross_platform scenarios — they must now use NO prices for dir1 or YES-ask prices for dir2.

---

### 3. `kalshi_side` hardcoded "YES" (`main.py:319`)

**Problem:** Dir2 cross-platform trades buy Kalshi NO (kalshi_action = "sell"), but `kalshi_side` is always logged as "YES". Reconciler uses this field to determine outcome direction, so dir2 P&L is computed with the wrong sign.

**Fix:**
```python
"kalshi_side": "YES" if gap.get("kalshi_action", "buy") == "buy" else "NO",
```

---

### 4. Polymarket WS URL default (`rust-core/src/types.rs:159`)

**Problem:** Default is `wss://ws-subscriptions.polymarket.com/ws/market`. Correct CLOB WebSocket endpoint is `wss://ws-subscriptions-clob.polymarket.com`. If `POLYMARKET_WS_URL` is not set in `.env`, Polymarket prices never arrive in cross-platform mode.

**Fix:** Update the `unwrap_or` default string. Also add `POLYMARKET_WS_URL` to `.env.example`.

---

### 5. FOK with GTC fallback (`polymarket_executor.py`)

**Problem:** `place_order` currently uses no explicit `order_type`, getting whatever the SDK default is. Within-leg partial fills are possible.

**Design:**
- `_place_sync` tries `OrderType.FOK` first.
- If response status is `"cancelled"` or order fill is zero AND `poly_liquidity_usdc > min_bet * 2` (book has depth for GTC to work), retry with `OrderType.GTC`.
- If book is thin (liquidity ≤ `min_bet * 2`), do not retry — raise `ExecutorError("FOK cancelled, book too thin")`.
- GTC path feeds into existing fill-poll loop.

**Config:** Add `USE_FOK=true` to `.env.example`. If false, skip FOK attempt entirely (useful for testing GTC behavior).

---

### 6. Zombie WebSocket detection (`rust-core/src/fetcher/polymarket.rs`)

**Problem:** Polymarket's CLOB WebSocket has a documented failure mode where TCP connection stays alive, PING/PONG works, but no book or price_change events arrive. Can persist for hours. Existing stale-REST-fallback helps at the token level but does not force a reconnect.

**Design:**
- Add `last_book_event: Instant` field initialized at connection open.
- Updated on every `book` or `price_change` message received (before processing).
- In the PING loop (already runs every ~30s), check `last_book_event.elapsed() > Duration::from_secs(30)`.
- If stale: log `WARN zombie_ws detected — forcing reconnect`, call `ws_stream.close()`.
- This drops into the existing exponential-backoff reconnect loop. REST stale-fallback (already present) continues feeding prices during the reconnect gap.

**What this does NOT change:** The existing REST stale-fallback still runs in parallel. Zombie detection is an additional reconnect trigger, not a replacement.

---

### 7. Startup validation (`main.py` + new `python-core/startup_check.py`)

**Problem:** Bot can start with empty API keys, unreachable exchanges, or a corrupted DB, then silently fail on first live trade attempt.

**Design:** New `startup_check.py` module, called from `main.py` before Rust subprocess spawns (and before any trade can execute). Checks:

1. `POLYMARKET_PRIVATE_KEY` and `POLYMARKET_WALLET_ADDRESS` non-empty (if not dry_run).
2. `KALSHI_API_KEY` and `KALSHI_API_SECRET` non-empty (if not dry_run).
3. Kalshi public API ping: `GET /trade-api/v2/exchange/status` → HTTP 200.
4. Polymarket CLOB ping: `GET /ok` → HTTP 200.
5. `trades.db` opens cleanly with `PRAGMA integrity_check` returning `"ok"`.

Any failure → `sys.exit(1)` with a one-line error naming the specific check that failed. Dry-run mode skips checks 1 and 2 but still runs 3–5.

**Interface:**
```python
# startup_check.py
async def run_all(config: dict) -> None:
    """Raises SystemExit(1) if any check fails."""
```

---

### 8. Edge-to-spread ratio gate (`detector.py`)

**Problem:** A 10¢ gap on a market with an 8¢ spread is only 2¢ net edge. Current validation checks gap size but not spread quality.

**Design:** Added as Check 1c in `detector.validate()`, after EV check and before gap stability check.

```python
# Spread computed from Rust Price fields already in gap via poly_liquidity calculation
# Gap dict has polymarket_price (NO price or YES ask) — spread requires both sides
# Use Kalshi side for now (has bid/ask in Price struct)
kalshi_spread_cents = gap.get("kalshi_spread_cents", None)
if kalshi_spread_cents is not None and kalshi_spread_cents > 0:
    ratio = gap_cents / kalshi_spread_cents
    if ratio < config.get("min_edge_to_spread_ratio", 3.0):
        return False, f"Edge/spread ratio {ratio:.2f} < 3.0 — gap eaten by spread"
```

**Rust change required:** `Gap` struct needs `kalshi_spread_cents: f64` field (computed as `(yes_ask - yes_price) * 100` in `check_cross_platform`). Pass `0.0` for internal pairs (Polymarket spread used instead).

**Config:** Add `MIN_EDGE_TO_SPREAD_RATIO=3.0` to `.env.example`. Set to `0.0` to disable.

---

## Data Flow

```
startup_check.py → [halt on fail]
    ↓
Rust process spawns (with corrected WS URL)
    ↓
Polymarket WS (zombie watchdog active)
    ↓
Gap emitted (with kalshi_spread_cents field)
    ↓
detector.validate()
  Check 0: dedup, kill switch, blacklist
  Check 0b: binary gate (internal only)
  Check 1: EV gate (FIXED combined = poly + kalshi)
  Check 1a: liquidity gate
  Check 1b: gap threshold per type
  Check 1c: edge/spread ratio gate (NEW)
  Check 2: stability (3 consecutive)
  Check 3: stale feed
  Check 4: resolution proximity
  Check 5: daily loss limit
  Check 6: position count
  Check 7: confidence
    ↓
TwoLegExecutor.execute()
  FOK attempt → GTC fallback (NEW)
  _gather_legs()
    leg_a fill verify (polymarket)
    leg_b fill verify (polymarket OR kalshi, FIXED)
  Emergency close on partial fill
    ↓
tracker.log_trade() (kalshi_side FIXED)
```

---

## Error Handling

- Zombie WS: handled by forced close → reconnect loop → REST fallback bridges the gap. No Python-side change needed.
- FOK cancel on thin book: `ExecutorError` raised → `_gather_legs` marks `a_ok=False, b_ok=False` → returns `None` → gap logged as failed, no trade.
- Startup check fail: `sys.exit(1)` with message. Clean exit, no partial state written.
- EV formula fix: no new error paths, only makes previously-rejected dir1 gaps visible.

---

## Testing

| Test | Location | What it verifies |
|------|----------|-----------------|
| `test_internal_legb_fill_verified` | `test_two_leg_executor.py` | Both legs polled for internal pairs |
| `test_dir1_gap_passes_ev_gate` | `test_detector.py` | Dir1 with NO price=0.39 no longer rejected |
| `test_kalshi_side_dir2_is_NO` | `test_tracker.py` | kalshi_side="NO" logged for sell action |
| `test_fok_falls_back_to_gtc` | `test_polymarket_executor.py` | FOK cancel + adequate depth → GTC retry |
| `test_fok_thin_book_aborts` | `test_polymarket_executor.py` | FOK cancel + thin book → ExecutorError |
| `test_startup_check_exits_on_bad_db` | `test_startup_check.py` | Corrupted DB triggers sys.exit(1) |
| `test_edge_spread_gate_rejects` | `test_detector.py` | ratio < 3.0 rejected |
| Existing Rust tests | `tests/comparator_tests.rs` | kalshi_spread_cents field present in Gap |
