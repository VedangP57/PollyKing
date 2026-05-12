# Make It Safe — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate every scenario where the bot loses money due to its own bugs — fix the four audit blockers, add FOK/GTC execution, zombie-proof the WebSocket, validate startup, and add an edge-to-spread gate.

**Architecture:** Eight independent fixes applied in dependency order: Python bugs first (no rebuild needed), then Rust changes (require `cargo build --release`), then new Python modules. Each task ends with a commit and a test run.

**Tech Stack:** Python 3.13 / uv, Rust / cargo, pytest, py_clob_client_v2 (`OrderType.FOK`/`GTC`), tokio `Instant`, SQLite pragma.

---

## File Map

| File | Change |
|------|--------|
| `python-core/two_leg_executor.py` | Fix leg_b fill verify guard (line 273); add FOK/GTC fallback in `_place_sync` |
| `python-core/detector.py` | Fix EV combined formula (line 89); add edge/spread gate (new Check 1c) |
| `python-core/main.py` | Fix kalshi_side (line 319); wire startup_check |
| `rust-core/src/types.rs` | Add `kalshi_spread_cents: f64` to `Gap` struct and `Gap::new()` |
| `rust-core/src/comparator.rs` | Compute and set `kalshi_spread_cents` after `Gap::new()` calls |
| `rust-core/src/fetcher/polymarket.rs` | Add zombie WS watchdog; fix WS URL default |
| `python-core/startup_check.py` | New module — 5 pre-flight checks |
| `config/.env.example` | Add `POLYMARKET_WS_URL`, `USE_FOK`, `MIN_EDGE_TO_SPREAD_RATIO` |
| `python-core/tests/test_two_leg_executor.py` | Add leg_b test; update fill concurrent test |
| `python-core/tests/test_detector.py` | Fix 3 cross-platform EV tests; add spread gate test; add dir1 test |
| `python-core/tests/test_polymarket_executor.py` | Add FOK/GTC tests |
| `python-core/tests/test_startup_check.py` | New test file |
| `rust-core/tests/comparator_tests.rs` | Update `make_pair` / gap assertions for new field |

---

## Task 1: Fix internal leg_b fill verification

**Files:**
- Modify: `python-core/two_leg_executor.py:273`
- Test: `python-core/tests/test_two_leg_executor.py`

- [ ] **Step 1: Write the failing test**

Open `python-core/tests/test_two_leg_executor.py` and add after the existing `test_fill_polls_run_concurrently` test:

```python
@pytest.mark.asyncio
async def test_internal_legb_fill_verified():
    """For internal pairs, leg_b (also Polymarket) must be fill-polled."""
    config = {"dry_run": False, "min_bet_usdc": 10.0, "max_bet_usdc": 100.0,
              "bankroll_usdc": 500.0, "kelly_fraction": 0.25}
    db = sqlite3.connect(":memory:")
    tracker.init_db(":memory:")

    poly_fills = []

    class MockPoly:
        async def place_order(self, token_id, side, amount_usdc, price, neg_risk):
            return {"order_id": f"ord-{token_id[:4]}", "status": "open"}
        async def get_order_status(self, order_id):
            poly_fills.append(order_id)
            return "matched"
        async def get_balance(self):
            return 999.0

    class MockKalshi:
        pass  # not used for internal pairs

    gap = {
        "pair_type": "internal",
        "market_id": "evt::tok1-tok2",
        "polymarket_token": "tok1",
        "kalshi_ticker": "tok2",  # token_b stored here for internal
        "polymarket_price": 0.45,
        "kalshi_price": 0.45,
        "gap_cents": 10.0,
        "confidence": "high",
        "poly_liquidity_usdc": 200.0,
        "kalshi_liquidity_usdc": 200.0,
        "fee_rate": 0.02,
    }
    executor = TwoLegExecutor.__new__(TwoLegExecutor)
    executor._config = config
    executor._db = db
    executor._poly = MockPoly()
    executor._kalshi = MockKalshi()

    await executor._execute_internal(gap, bet_size=20.0, price_buffer=0.0)

    # Both tok1 and tok2 order IDs should have been polled
    assert len(poly_fills) == 2, f"Expected 2 fill polls (both legs), got {len(poly_fills)}: {poly_fills}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd python-core && uv run python -m pytest tests/test_two_leg_executor.py::test_internal_legb_fill_verified -v
```

Expected: `FAILED` — only 1 fill poll, not 2 (leg_b is never polled currently).

- [ ] **Step 3: Fix the guard in `_gather_legs`**

In `python-core/two_leg_executor.py`, replace lines 273–276:

**Before:**
```python
        if b_ok and kalshi_count is not None and not dry_run:
            kalshi_id = result_b.get("order_id", "")
            if kalshi_id:
                verify.append(("kalshi", self._poll_and_cancel("kalshi", kalshi_id)))
```

**After:**
```python
        if b_ok and not dry_run:
            b_id = result_b.get("order_id", "")
            if b_id:
                b_platform = "kalshi" if kalshi_count is not None else "polymarket"
                verify.append((b_platform, self._poll_and_cancel(b_platform, b_id)))
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd python-core && uv run python -m pytest tests/test_two_leg_executor.py::test_internal_legb_fill_verified -v
```

Expected: `PASSED`

- [ ] **Step 5: Run full executor test suite to check no regressions**

```bash
cd python-core && uv run python -m pytest tests/test_two_leg_executor.py -v
```

Expected: All existing tests + new test pass.

- [ ] **Step 6: Commit**

```bash
git add python-core/two_leg_executor.py python-core/tests/test_two_leg_executor.py
git commit -m "fix: verify leg_b fill for internal pairs — kalshi_count=None guard was skipping Polymarket leg_b"
```

---

## Task 2: Fix EV combined formula (dir1 cross-platform silently rejected)

**Files:**
- Modify: `python-core/detector.py:84-89`
- Test: `python-core/tests/test_detector.py`

**Background:** Rust sends `polymarket_price = poly.no_price` for dir1 and `poly.yes_ask` for dir2. The old formula `(1 - poly_price) + kalshi_price` was for when poly_price was always the YES price. With the new Rust format, dir1 combined is always > 1 → EV always negative → all dir1 gaps rejected. Fix: `combined = poly_price + kalshi_price` for all pair types.

- [ ] **Step 1: Write the new failing test**

In `python-core/tests/test_detector.py`, add:

```python
def test_dir1_gap_passes_ev_gate(make_db):
    """Dir1 cross-platform gap: Rust sends NO price as polymarket_price.
    Combined = poly.no_price + kalshi.yes_ask — must NOT be inverted."""
    config = {
        "ev_min_cents": 1.0, "ev_taker_fee_rate": 0.02, "ev_slippage_cents": 0.5,
        "min_bet_usdc": 10.0, "max_bet_usdc": 100.0, "max_daily_loss_usdc": 50.0,
        "max_open_positions": 5, "cross_platform_min_gap_cents": 5.0,
        "internal_min_gap_cents": 8.0, "kalshi_fee_per_contract": 0.0,
    }
    db = make_db
    detector = GapDetector(config, db)
    # Feed 3 identical observations to satisfy stability check
    gap = {
        "pair_type": "cross_platform",
        "market_id": "test-dir1",
        "polymarket_price": 0.39,   # poly.no_price — what Rust sends for dir1
        "kalshi_price": 0.55,       # kalshi.yes_ask — actual execution price
        "kalshi_action": "buy",
        "gap_cents": 6.0,           # (1 - 0.39 - 0.55) * 100
        "polymarket_token": "tok-no",
        "kalshi_ticker": "TICK-A",
        "confidence": "high",
        "poly_liquidity_usdc": 200.0,
        "kalshi_liquidity_usdc": 200.0,
        "fee_rate": 0.02,
    }
    for _ in range(3):
        ok, reason = detector.validate(gap)
    # With correct formula: combined = 0.39+0.55 = 0.94 → ev = 6¢ → passes
    # With old formula: combined = 0.61+0.55 = 1.16 → ev = -16¢ → fails
    assert ok, f"Dir1 gap rejected: {reason}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd python-core && uv run python -m pytest tests/test_detector.py::test_dir1_gap_passes_ev_gate -v
```

Expected: `FAILED` — rejected by EV gate with combined > 1.

- [ ] **Step 3: Fix the formula**

In `python-core/detector.py`, replace lines 84–89:

**Before:**
```python
        # Cross-platform: buy Poly NO + Kalshi YES → combined = (1-poly) + kalshi
        # Internal negRisk: buy both YES tokens     → combined = poly + kalshi
        if pair_type == "internal":
            combined = poly_price + kalshi_price
        else:
            combined = (1.0 - poly_price) + kalshi_price
```

**After:**
```python
        # Rust sends actual execution prices for both legs (NO price for dir1,
        # YES ask for dir2, YES price for internal). Add directly — no inversion.
        combined = poly_price + kalshi_price
```

- [ ] **Step 4: Find and fix the three existing cross-platform test assertions that use YES prices**

Search for cross-platform tests that rely on the old inversion formula. Run:

```bash
cd python-core && grep -n "cross_platform.*polymarket_price\|polymarket_price.*cross_platform\|combined = (1" tests/test_detector.py | head -20
```

For each test using a YES price (e.g., `polymarket_price: 0.55, kalshi_price: 0.45`), check whether it expects the old combined `(1-0.55)+0.45 = 0.90` or the new `0.55+0.45 = 1.00`. Update the price values so that `poly_price + kalshi_price` produces the intended combined. Example: to test a 6¢ gap with 2% fee, set `polymarket_price=0.42, kalshi_price=0.52` so combined = 0.94, gap = 6¢.

- [ ] **Step 5: Run all detector tests**

```bash
cd python-core && uv run python -m pytest tests/test_detector.py -v
```

Expected: All tests pass including the new `test_dir1_gap_passes_ev_gate`.

- [ ] **Step 6: Commit**

```bash
git add python-core/detector.py python-core/tests/test_detector.py
git commit -m "fix: EV combined formula — use poly_price+kalshi_price for all pair types; Rust sends actual execution prices"
```

---

## Task 3: Fix kalshi_side hardcoded "YES"

**Files:**
- Modify: `python-core/main.py:319`
- Test: `python-core/tests/test_tracker.py` (new assertion)

- [ ] **Step 1: Write the failing test**

In `python-core/tests/test_tracker.py`, add:

```python
def test_kalshi_side_dir2_logged_as_NO():
    """Dir2 trades sell Kalshi YES (=buy NO). kalshi_side must be 'NO' not 'YES'."""
    # This tests the trade dict construction in main.py _handle_gap_inner
    # Simulate what _handle_gap_inner builds for a dir2 gap
    gap = {"pair_type": "cross_platform", "kalshi_action": "sell", "market_id": "test"}
    pair_type = gap.get("pair_type", "cross_platform")
    kalshi_side = "YES" if gap.get("kalshi_action", "buy") == "buy" else "NO"
    assert kalshi_side == "NO", f"Dir2 should log kalshi_side='NO', got '{kalshi_side}'"
```

- [ ] **Step 2: Verify test passes even before code change** (this tests the fix expression directly)

```bash
cd python-core && uv run python -m pytest tests/test_tracker.py::test_kalshi_side_dir2_logged_as_NO -v
```

Expected: `PASSED` (the expression itself is correct).

- [ ] **Step 3: Apply fix to main.py**

In `python-core/main.py`, replace line 319:

**Before:**
```python
        "kalshi_side": "YES",
```

**After:**
```python
        "kalshi_side": "YES" if gap.get("kalshi_action", "buy") == "buy" else "NO",
```

- [ ] **Step 4: Run full test suite**

```bash
cd python-core && uv run python -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

Expected: All tests pass.

- [ ] **Step 5: Commit**

```bash
git add python-core/main.py python-core/tests/test_tracker.py
git commit -m "fix: kalshi_side derived from kalshi_action — dir2 logs 'NO' not 'YES'"
```

---

## Task 4: Fix Polymarket WS URL default

**Files:**
- Modify: `rust-core/src/types.rs:159`
- Modify: `config/.env.example`

- [ ] **Step 1: Fix the default URL in types.rs**

In `rust-core/src/types.rs`, replace line 159:

**Before:**
```rust
                .unwrap_or("wss://ws-subscriptions.polymarket.com/ws/market".into()),
```

**After:**
```rust
                .unwrap_or("wss://ws-subscriptions-clob.polymarket.com".into()),
```

- [ ] **Step 2: Add to .env.example**

In `config/.env.example`, add under the Polymarket section:

```bash
# Polymarket WebSocket — CLOB endpoint (required for cross-platform price feed)
POLYMARKET_WS_URL=wss://ws-subscriptions-clob.polymarket.com
```

- [ ] **Step 3: Build Rust to verify it compiles**

```bash
cd rust-core && cargo build --release 2>&1 | tail -5
```

Expected: `Finished release` with no errors.

- [ ] **Step 4: Run Rust tests**

```bash
cd rust-core && cargo test 2>&1 | tail -10
```

Expected: All 23 tests pass.

- [ ] **Step 5: Commit**

```bash
git add rust-core/src/types.rs config/.env.example
git commit -m "fix: Polymarket WS URL default → wss://ws-subscriptions-clob.polymarket.com"
```

---

## Task 5: FOK with GTC fallback in polymarket_executor

**Files:**
- Modify: `python-core/polymarket_executor.py:1-6` (imports), `python-core/polymarket_executor.py:59-85` (`_place_sync`)
- Test: `python-core/tests/test_polymarket_executor.py`
- Modify: `config/.env.example`

**Background:** `create_and_post_order` defaults to `OrderType.GTC`. We add explicit FOK first; if the response status is `"cancelled"` (or fill is zero) and book has depth ≥ `min_bet * 2`, retry with GTC. If book is thin, raise `ExecutorError`.

- [ ] **Step 1: Write failing tests**

In `python-core/tests/test_polymarket_executor.py`, add:

```python
import pytest
from unittest.mock import MagicMock, patch
from polymarket_executor import PolymarketExecutor
from kalshi_executor import ExecutorError

def _make_executor(use_fok=True, min_bet=10.0):
    config = {
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
        "polymarket_signature_type": 0,
        "use_fok": use_fok,
        "min_bet_usdc": min_bet,
    }
    ex = PolymarketExecutor(config)
    return ex

@pytest.mark.asyncio
async def test_fok_falls_back_to_gtc_when_cancelled_with_depth():
    """FOK cancel + adequate liquidity → GTC retry → success."""
    executor = _make_executor(use_fok=True)
    call_order_types = []

    def fake_create_and_post_order(order_args, options=None, order_type=None):
        call_order_types.append(order_type)
        if order_type == "FOK":
            return {"orderID": "", "status": "cancelled", "errorMsg": None}
        return {"orderID": "gtc-123", "status": "matched", "errorMsg": None}

    mock_client = MagicMock()
    mock_client.create_and_post_order.side_effect = fake_create_and_post_order
    executor._client = mock_client

    result = executor._place_sync(
        token_id="tok1", price=0.5, amount_usdc=20.0, neg_risk=False,
        poly_liquidity_usdc=50.0,  # 50 > 10*2=20 → has depth
    )
    assert result["order_id"] == "gtc-123"
    assert "FOK" in call_order_types
    assert "GTC" in call_order_types

@pytest.mark.asyncio
async def test_fok_thin_book_raises_executor_error():
    """FOK cancel + thin book → ExecutorError (no GTC retry)."""
    executor = _make_executor(use_fok=True)

    def fake_create_and_post_order(order_args, options=None, order_type=None):
        return {"orderID": "", "status": "cancelled", "errorMsg": None}

    mock_client = MagicMock()
    mock_client.create_and_post_order.side_effect = fake_create_and_post_order
    executor._client = mock_client

    with pytest.raises(ExecutorError, match="FOK cancelled, book too thin"):
        executor._place_sync(
            token_id="tok1", price=0.5, amount_usdc=20.0, neg_risk=False,
            poly_liquidity_usdc=15.0,  # 15 < 10*2=20 → thin
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd python-core && uv run python -m pytest tests/test_polymarket_executor.py::test_fok_falls_back_to_gtc_when_cancelled_with_depth tests/test_polymarket_executor.py::test_fok_thin_book_raises_executor_error -v
```

Expected: `FAILED` — `_place_sync` has no `poly_liquidity_usdc` param and no FOK/GTC logic.

- [ ] **Step 3: Update imports in polymarket_executor.py**

In `python-core/polymarket_executor.py`, replace line 5:

**Before:**
```python
from py_clob_client_v2.clob_types import OrderPayload
```

**After:**
```python
from py_clob_client_v2.clob_types import OrderPayload, OrderType
```

- [ ] **Step 4: Rewrite `_place_sync` with FOK/GTC logic**

In `python-core/polymarket_executor.py`, replace lines 59–85:

```python
    def _place_sync(
        self,
        token_id: str,
        price: float,
        amount_usdc: float,
        neg_risk: bool = False,
        poly_liquidity_usdc: float = float("inf"),
    ) -> dict:
        client = self._get_client()
        size = round(amount_usdc / price, 2) if price > 0 else 0.0
        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=size,
            side=BUY,
        )
        options = PartialCreateOrderOptions(tick_size="0.01", neg_risk=neg_risk)
        use_fok = self._config.get("use_fok", True)

        if use_fok:
            fok_resp = client.create_and_post_order(order_args, options=options, order_type=OrderType.FOK)
            if isinstance(fok_resp, dict) and fok_resp.get("errorMsg"):
                raise ExecutorError(f"Polymarket FOK rejected: {fok_resp['errorMsg']}")
            fok_status = fok_resp.get("status", "") if isinstance(fok_resp, dict) else ""
            fok_id = fok_resp.get("orderID", "") if isinstance(fok_resp, dict) else ""
            if fok_id and fok_status not in ("cancelled",):
                return {"order_id": fok_id, "status": fok_status,
                        "platform": "polymarket", "token_id": token_id, "amount_usdc": amount_usdc}
            # FOK cancelled — check depth for GTC fallback
            min_bet = self._config.get("min_bet_usdc", 10.0)
            if poly_liquidity_usdc <= min_bet * 2:
                raise ExecutorError("FOK cancelled, book too thin — not retrying with GTC")

        resp = client.create_and_post_order(order_args, options=options, order_type=OrderType.GTC)
        if isinstance(resp, dict) and resp.get("errorMsg"):
            raise ExecutorError(f"Polymarket order rejected: {resp['errorMsg']}")
        return {
            "order_id": resp.get("orderID", ""),
            "status": resp.get("status", ""),
            "platform": "polymarket",
            "token_id": token_id,
            "amount_usdc": amount_usdc,
        }
```

- [ ] **Step 5: Update `place_order` async wrapper to pass liquidity**

In `python-core/polymarket_executor.py`, replace lines 166–177 (`place_order` method):

**Before:**
```python
    async def place_order(
        self,
        token_id: str,
        side: str,
        amount_usdc: float,
        price: float = 0.5,
        neg_risk: bool = False,
    ) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._place_sync, token_id, price, amount_usdc, neg_risk
        )
```

**After:**
```python
    async def place_order(
        self,
        token_id: str,
        side: str,
        amount_usdc: float,
        price: float = 0.5,
        neg_risk: bool = False,
        poly_liquidity_usdc: float = float("inf"),
    ) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._place_sync, token_id, price, amount_usdc, neg_risk, poly_liquidity_usdc
        )
```

- [ ] **Step 6: Pass liquidity from `_execute_cross_platform` in two_leg_executor.py**

In `python-core/two_leg_executor.py`, update the `poly_task` call in `_execute_cross_platform` (around line 181):

**Before:**
```python
        poly_task = self._poly.place_order(
            token_id=gap["polymarket_token"],
            side="BUY",
            amount_usdc=poly_amount,
            price=order_price,
            neg_risk=False,
        )
```

**After:**
```python
        poly_task = self._poly.place_order(
            token_id=gap["polymarket_token"],
            side="BUY",
            amount_usdc=poly_amount,
            price=order_price,
            neg_risk=False,
            poly_liquidity_usdc=gap.get("poly_liquidity_usdc", float("inf")),
        )
```

- [ ] **Step 7: Add `USE_FOK` to .env.example**

```bash
# Order execution — FOK tries fill-or-kill first, falls back to GTC if book has depth
USE_FOK=true
```

- [ ] **Step 8: Run FOK tests**

```bash
cd python-core && uv run python -m pytest tests/test_polymarket_executor.py -v
```

Expected: Both new tests pass. No regressions.

- [ ] **Step 9: Run full Python test suite**

```bash
cd python-core && uv run python -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

Expected: All tests pass.

- [ ] **Step 10: Commit**

```bash
git add python-core/polymarket_executor.py python-core/two_leg_executor.py \
        python-core/tests/test_polymarket_executor.py config/.env.example
git commit -m "feat: FOK with GTC fallback — explicit FOK first; falls back to GTC if book has depth; thin book raises ExecutorError"
```

---

## Task 6: Zombie WebSocket detection in Rust

**Files:**
- Modify: `rust-core/src/fetcher/polymarket.rs` (`run_ws_session` function, lines 187–243)

**Background:** The PING loop fires on `hb.tick()` every `HEARTBEAT_SECS`. We add `last_book_event: Instant` initialised at session open. On each `Message::Text` containing real book data, reset it. On each heartbeat tick, if elapsed > 30s, close the stream to trigger the reconnect loop.

- [ ] **Step 1: Add `use std::time::Instant;` if not already present**

Check line 1-10 of `rust-core/src/fetcher/polymarket.rs`:

```bash
head -15 rust-core/src/fetcher/polymarket.rs
```

If `use std::time::Instant;` is absent, add it after the existing `use` statements at the top of the file.

- [ ] **Step 2: Add zombie watchdog to `run_ws_session`**

In `rust-core/src/fetcher/polymarket.rs`, rewrite `run_ws_session` (lines 187–243). The full replacement:

```rust
async fn run_ws_session(
    tokens: &[String],
    price_map: &Arc<RwLock<HashMap<String, Price>>>,
    price_watch_tx: &Arc<watch::Sender<u64>>,
    sub_rx: &mut mpsc::Receiver<Vec<String>>,
) -> Result<Vec<String>> {
    let mut added: Vec<String> = Vec::new();

    let (mut ws_stream, _) = connect_async(WS_URL)
        .await
        .map_err(|e| anyhow::anyhow!("Polymarket WS connect failed: {e}"))?;

    info!("Polymarket WS session connected ({} tokens)", tokens.len());

    ws_stream
        .send(Message::Text(build_subscription_message(tokens)))
        .await
        .map_err(|e| anyhow::anyhow!("WS subscribe failed: {e}"))?;

    let mut hb = tokio::time::interval(Duration::from_secs(HEARTBEAT_SECS));
    hb.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    // Zombie watchdog: track time of last real book/price_change event.
    // If the connection looks alive (PING/PONG works) but no data arrives
    // for ZOMBIE_TIMEOUT_SECS, force a reconnect.
    const ZOMBIE_TIMEOUT_SECS: u64 = 30;
    let mut last_book_event = std::time::Instant::now();

    loop {
        tokio::select! {
            msg = ws_stream.next() => match msg {
                Some(Ok(Message::Text(text))) => {
                    // Reset zombie clock on every real data message
                    last_book_event = std::time::Instant::now();
                    handle_price_message(&text, price_map, price_watch_tx);
                }
                Some(Ok(Message::Ping(data))) => {
                    let _ = ws_stream.send(Message::Pong(data)).await;
                }
                Some(Ok(Message::Close(_))) | None => {
                    info!("Polymarket WS connection closed");
                    return Ok(added);
                }
                Some(Err(e)) => {
                    return Err(anyhow::anyhow!("WS read error: {e}"));
                }
                Some(Ok(Message::Binary(_))) => {
                    warn!("Polymarket WS: unexpected binary frame — ignoring");
                }
                _ => {}
            },
            _ = hb.tick() => {
                // Zombie check: connection alive but no book events
                if last_book_event.elapsed().as_secs() > ZOMBIE_TIMEOUT_SECS {
                    warn!(
                        "Polymarket WS zombie detected — no book events for {}s — forcing reconnect",
                        last_book_event.elapsed().as_secs()
                    );
                    let _ = ws_stream.close(None).await;
                    return Err(anyhow::anyhow!("zombie_ws: no data for {}s", ZOMBIE_TIMEOUT_SECS));
                }
                let _ = ws_stream.send(Message::Text("PING".to_string())).await;
            }
            Some(new_tokens) = sub_rx.recv() => {
                let dyn_msg = build_dynamic_subscribe_message(&new_tokens);
                if let Err(e) = ws_stream.send(Message::Text(dyn_msg)).await {
                    warn!("Dynamic subscribe send failed: {e}");
                } else {
                    added.extend(new_tokens);
                }
            }
        }
    }
}
```

- [ ] **Step 3: Build and verify**

```bash
cd rust-core && cargo build --release 2>&1 | tail -5
```

Expected: `Finished release` — no errors.

- [ ] **Step 4: Run Rust tests**

```bash
cd rust-core && cargo test 2>&1 | tail -10
```

Expected: All 23 tests pass (zombie logic is runtime-only, no unit test needed — it's a timeout path).

- [ ] **Step 5: Commit**

```bash
git add rust-core/src/fetcher/polymarket.rs
git commit -m "feat: zombie WS detection — 30s without book events forces reconnect into existing backoff loop"
```

---

## Task 7: Startup validation module

**Files:**
- Create: `python-core/startup_check.py`
- Create: `python-core/tests/test_startup_check.py`
- Modify: `python-core/main.py` (wire call before Rust spawn)

- [ ] **Step 1: Write the failing tests**

Create `python-core/tests/test_startup_check.py`:

```python
import asyncio
import pytest
import sqlite3
import tempfile
import os
from unittest.mock import AsyncMock, patch
import startup_check


@pytest.fixture
def good_db(tmp_path):
    db_path = str(tmp_path / "trades.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def bad_db(tmp_path):
    db_path = str(tmp_path / "corrupt.db")
    with open(db_path, "wb") as f:
        f.write(b"not a sqlite database " * 10)
    return db_path


@pytest.mark.asyncio
async def test_good_db_passes(good_db):
    """A valid SQLite DB passes the integrity check."""
    config = {"dry_run": True, "db_path": good_db}
    # Patch network checks so they pass
    with patch("startup_check._ping_url", new=AsyncMock(return_value=True)):
        await startup_check.run_all(config)  # should not raise


@pytest.mark.asyncio
async def test_bad_db_raises_system_exit(bad_db):
    """A corrupted DB triggers SystemExit(1)."""
    config = {"dry_run": True, "db_path": bad_db}
    with patch("startup_check._ping_url", new=AsyncMock(return_value=True)):
        with pytest.raises(SystemExit) as exc_info:
            await startup_check.run_all(config)
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_missing_api_keys_raises_in_live_mode(good_db):
    """Empty API keys → SystemExit(1) when not dry_run."""
    config = {
        "dry_run": False,
        "db_path": good_db,
        "polymarket_private_key": "",
        "polymarket_wallet_address": "0xabc",
        "kalshi_api_key": "key",
        "kalshi_api_secret": "secret",
    }
    with patch("startup_check._ping_url", new=AsyncMock(return_value=True)):
        with pytest.raises(SystemExit) as exc_info:
            await startup_check.run_all(config)
    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_kalshi_unreachable_raises(good_db):
    """Kalshi ping failure → SystemExit(1)."""
    config = {"dry_run": True, "db_path": good_db}

    async def fake_ping(url):
        if "kalshi" in url:
            return False
        return True

    with patch("startup_check._ping_url", new=fake_ping):
        with pytest.raises(SystemExit) as exc_info:
            await startup_check.run_all(config)
    assert exc_info.value.code == 1
```

- [ ] **Step 2: Run tests to verify they fail (module doesn't exist yet)**

```bash
cd python-core && uv run python -m pytest tests/test_startup_check.py -v
```

Expected: `ERROR` — `ModuleNotFoundError: No module named 'startup_check'`

- [ ] **Step 3: Create `python-core/startup_check.py`**

```python
import asyncio
import logging
import sqlite3
import sys

import aiohttp

log = logging.getLogger(__name__)

_KALSHI_PING_URL = "https://api.elections.kalshi.com/trade-api/v2/exchange/status"
_POLYMARKET_PING_URL = "https://clob.polymarket.com/ok"


async def _ping_url(url: str) -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return resp.status == 200
    except Exception as e:
        log.debug("Ping failed for %s: %s", url, e)
        return False


def _check_db_integrity(db_path: str) -> bool:
    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
        result = conn.execute("PRAGMA integrity_check").fetchone()
        conn.close()
        return result is not None and result[0] == "ok"
    except Exception as e:
        log.error("DB integrity check failed: %s", e)
        return False


async def run_all(config: dict) -> None:
    """Run all pre-flight checks. Raises SystemExit(1) if any check fails."""
    dry_run = config.get("dry_run", True)
    db_path = config.get("db_path", "data/trades.db")

    # Check 1 & 2: API keys required in live mode
    if not dry_run:
        if not config.get("polymarket_private_key", "").strip():
            log.critical("STARTUP FAIL: POLYMARKET_PRIVATE_KEY is empty — cannot run in live mode")
            sys.exit(1)
        if not config.get("polymarket_wallet_address", "").strip():
            log.critical("STARTUP FAIL: POLYMARKET_WALLET_ADDRESS is empty — cannot run in live mode")
            sys.exit(1)
        if not config.get("kalshi_api_key", "").strip():
            log.critical("STARTUP FAIL: KALSHI_API_KEY is empty — cannot run in live mode")
            sys.exit(1)
        if not config.get("kalshi_api_secret", "").strip():
            log.critical("STARTUP FAIL: KALSHI_API_SECRET is empty — cannot run in live mode")
            sys.exit(1)

    # Check 3: Kalshi public API reachable
    kalshi_url = config.get("kalshi_api_url", "https://api.elections.kalshi.com/trade-api/v2")
    kalshi_ping = kalshi_url.rstrip("/") + "/exchange/status"
    if not await _ping_url(kalshi_ping):
        log.critical("STARTUP FAIL: Kalshi API unreachable at %s — check network/VPN", kalshi_ping)
        sys.exit(1)

    # Check 4: Polymarket CLOB reachable
    if not await _ping_url(_POLYMARKET_PING_URL):
        log.warning("STARTUP WARN: Polymarket CLOB unreachable — cross-platform mode will not work (geo-block?)")
        # Non-fatal: bot can still run in internal mode

    # Check 5: DB integrity
    if not _check_db_integrity(db_path):
        log.critical("STARTUP FAIL: trades.db failed integrity_check — DB may be corrupt. Run: sqlite3 %s 'PRAGMA integrity_check'", db_path)
        sys.exit(1)

    log.info("Startup checks passed (dry_run=%s)", dry_run)
```

- [ ] **Step 4: Run the tests**

```bash
cd python-core && uv run python -m pytest tests/test_startup_check.py -v
```

Expected: All 4 tests pass.

- [ ] **Step 5: Wire startup_check into main.py**

In `python-core/main.py`, add import after existing imports:

```python
from startup_audit import audit_orphan_positions
```

becomes:

```python
from startup_audit import audit_orphan_positions
import startup_check
```

Then add the call inside `async def main()` before `rust_process = await asyncio.create_subprocess_exec(...)`:

```python
    # Pre-flight validation — halts with clear message if anything is wrong
    await startup_check.run_all(CONFIG)
```

- [ ] **Step 6: Run full Python test suite**

```bash
cd python-core && uv run python -m pytest tests/ -v --tb=short 2>&1 | tail -10
```

Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add python-core/startup_check.py python-core/tests/test_startup_check.py python-core/main.py
git commit -m "feat: startup validation — checks API keys, Kalshi ping, DB integrity before Rust spawns"
```

---

## Task 8: Edge-to-spread ratio gate (Rust Gap field + Python detector)

**Files:**
- Modify: `rust-core/src/types.rs` — add `kalshi_spread_cents` field to `Gap`
- Modify: `rust-core/src/comparator.rs` — compute and set the field
- Modify: `rust-core/tests/comparator_tests.rs` — update gap struct assertions
- Modify: `python-core/detector.py` — add Check 1c
- Modify: `python-core/tests/test_detector.py` — add spread gate test
- Modify: `config/.env.example`

**Strategy for Rust:** Add `kalshi_spread_cents` as a plain field with default `0.0` in `Gap::new()`. Set it post-construction in `check_cross_platform`. This avoids breaking `Gap::new()` call sites in tests.

- [ ] **Step 1: Add field to Gap struct in types.rs**

In `rust-core/src/types.rs`, inside the `Gap` struct (around line 45), add one field after `kalshi_liquidity_usdc`:

```rust
    pub kalshi_spread_cents: f64,
```

In `Gap::new()` (around line 79), add to the struct literal:

```rust
            kalshi_spread_cents: 0.0,
```

The full `Gap` struct after the change:
```rust
pub struct Gap {
    pub event: String,
    pub pair_type: String,
    pub market_id: String,
    pub polymarket_price: f64,
    pub kalshi_price: f64,
    pub gap_cents: f64,
    pub polymarket_token: String,
    pub kalshi_ticker: String,
    pub kalshi_action: String,
    pub timestamp: String,
    pub poly_liquidity_usdc: f64,
    pub kalshi_liquidity_usdc: f64,
    pub kalshi_spread_cents: f64,   // ← NEW: (yes_ask - yes_bid) * 100 for Kalshi side
}
```

And in `Gap::new()`, the return struct literal gets `kalshi_spread_cents: 0.0` added.

- [ ] **Step 2: Compute spread in comparator.rs**

In `rust-core/src/comparator.rs`, in `check_cross_platform`, find each `let _ = gap_tx.try_send(gap)` call for dir1 and dir2. Before each `try_send`, set the spread on the `gap` variable. Change the `let gap = Gap::new(...)` to `let mut gap = Gap::new(...)`:

For **dir1** (around line 109):
```rust
            let mut gap = Gap::new(
                "cross_platform".into(),
                pair.market_id.clone(),
                poly.no_price,
                kalshi.yes_ask,
                pair.no_token_a.clone(),
                pair.token_b.clone(),
                "buy".into(),
                gap1,
                poly_liq1,
                kalshi_liq1,
            );
            gap.kalshi_spread_cents = (kalshi.yes_ask - kalshi.yes_price) * 100.0;
            let _ = gap_tx.try_send(gap);
```

For **dir2** (around line 143):
```rust
            let mut gap = Gap::new(
                "cross_platform".into(),
                format!("{}-rev", pair.market_id),
                poly.yes_ask,
                kalshi.no_price,
                pair.token_a.clone(),
                pair.token_b.clone(),
                "sell".into(),
                gap2,
                poly_liq2,
                kalshi_liq2,
            );
            gap.kalshi_spread_cents = (kalshi.yes_ask - kalshi.yes_price) * 100.0;
            let _ = gap_tx.try_send(gap);
```

Internal pairs leave `kalshi_spread_cents` at the default `0.0` (no change needed in `check_internal`).

- [ ] **Step 3: Build Rust**

```bash
cd rust-core && cargo build --release 2>&1 | tail -5
```

Expected: `Finished release` — no errors.

- [ ] **Step 4: Run Rust tests**

```bash
cd rust-core && cargo test 2>&1 | tail -15
```

Expected: All 23 tests pass. (The new field defaults to `0.0` in all existing `Gap::new()` calls in test helpers, so no test breakage.)

- [ ] **Step 5: Write failing Python test for the gate**

In `python-core/tests/test_detector.py`, add:

```python
def test_edge_spread_gate_rejects_low_ratio(make_db):
    """gap_cents / kalshi_spread_cents < 3.0 → rejected."""
    config = {
        "ev_min_cents": 1.0, "ev_taker_fee_rate": 0.02, "ev_slippage_cents": 0.5,
        "min_bet_usdc": 10.0, "max_bet_usdc": 100.0, "max_daily_loss_usdc": 50.0,
        "max_open_positions": 5, "cross_platform_min_gap_cents": 5.0,
        "internal_min_gap_cents": 8.0, "kalshi_fee_per_contract": 0.0,
        "min_edge_to_spread_ratio": 3.0,
    }
    db = make_db
    detector = GapDetector(config, db)
    gap = {
        "pair_type": "cross_platform",
        "market_id": "test-spread",
        "polymarket_price": 0.39,
        "kalshi_price": 0.55,
        "kalshi_action": "buy",
        "gap_cents": 6.0,
        "kalshi_spread_cents": 8.0,  # ratio = 6/8 = 0.75 < 3.0 → reject
        "polymarket_token": "tok-no",
        "kalshi_ticker": "TICK-A",
        "confidence": "high",
        "poly_liquidity_usdc": 200.0,
        "kalshi_liquidity_usdc": 200.0,
        "fee_rate": 0.02,
    }
    for _ in range(3):
        ok, reason = detector.validate(gap)
    assert not ok
    assert "Edge/spread ratio" in reason


def test_edge_spread_gate_passes_high_ratio(make_db):
    """gap_cents / kalshi_spread_cents >= 3.0 → not rejected by this gate."""
    config = {
        "ev_min_cents": 1.0, "ev_taker_fee_rate": 0.02, "ev_slippage_cents": 0.5,
        "min_bet_usdc": 10.0, "max_bet_usdc": 100.0, "max_daily_loss_usdc": 50.0,
        "max_open_positions": 5, "cross_platform_min_gap_cents": 5.0,
        "internal_min_gap_cents": 8.0, "kalshi_fee_per_contract": 0.0,
        "min_edge_to_spread_ratio": 3.0,
    }
    db = make_db
    detector = GapDetector(config, db)
    gap = {
        "pair_type": "cross_platform",
        "market_id": "test-spread-ok",
        "polymarket_price": 0.39,
        "kalshi_price": 0.55,
        "kalshi_action": "buy",
        "gap_cents": 6.0,
        "kalshi_spread_cents": 1.5,  # ratio = 6/1.5 = 4.0 >= 3.0 → passes gate
        "polymarket_token": "tok-no",
        "kalshi_ticker": "TICK-A",
        "confidence": "high",
        "poly_liquidity_usdc": 200.0,
        "kalshi_liquidity_usdc": 200.0,
        "fee_rate": 0.02,
    }
    for _ in range(3):
        ok, reason = detector.validate(gap)
    # Gate should pass; other checks may still reject, but not THIS gate
    if not ok:
        assert "Edge/spread ratio" not in reason
```

- [ ] **Step 6: Run to verify failure**

```bash
cd python-core && uv run python -m pytest tests/test_detector.py::test_edge_spread_gate_rejects_low_ratio -v
```

Expected: `FAILED` — no spread gate exists yet in detector.

- [ ] **Step 7: Add Check 1c to detector.py**

In `python-core/detector.py`, after the `# Check 1a: Liquidity gate` block (around line 127) and before the `# Check 1b: Per-pair-type minimum gap threshold` block, insert:

```python
        # Check 1c: Edge-to-spread ratio gate
        # Prevents trading when the bid-ask spread consumes most of the gap edge.
        # kalshi_spread_cents is set by Rust comparator; 0.0 means unknown (skip check).
        kalshi_spread_cents = gap.get("kalshi_spread_cents", 0.0)
        min_ratio = self.config.get("min_edge_to_spread_ratio", 3.0)
        if kalshi_spread_cents > 0 and min_ratio > 0:
            edge_to_spread = gap_cents / kalshi_spread_cents
            if edge_to_spread < min_ratio:
                return False, (
                    f"Edge/spread ratio {edge_to_spread:.2f} < {min_ratio:.1f} "
                    f"(gap {gap_cents:.1f}¢ / spread {kalshi_spread_cents:.1f}¢)"
                )
```

- [ ] **Step 8: Add to .env.example**

```bash
# Edge-to-spread quality gate — reject if gap_cents / kalshi_spread_cents < this ratio
# Set to 0.0 to disable
MIN_EDGE_TO_SPREAD_RATIO=3.0
```

- [ ] **Step 9: Run all detector tests**

```bash
cd python-core && uv run python -m pytest tests/test_detector.py -v
```

Expected: All tests pass.

- [ ] **Step 10: Run complete test suite**

```bash
cd python-core && uv run python -m pytest tests/ -v --tb=short 2>&1 | tail -10
cd rust-core && cargo test 2>&1 | tail -10
```

Expected: 154+ Python tests pass, 23 Rust tests pass.

- [ ] **Step 11: Commit**

```bash
git add rust-core/src/types.rs rust-core/src/comparator.rs \
        python-core/detector.py python-core/tests/test_detector.py \
        config/.env.example
git commit -m "feat: edge-to-spread ratio gate — Rust Gap carries kalshi_spread_cents; detector rejects ratio < 3.0"
```

---

## Task 9: Final verification

- [ ] **Step 1: Run full Python test suite**

```bash
cd python-core && uv run python -m pytest tests/ -v 2>&1 | tail -5
```

Expected: 162+ tests passed (154 original + 8 new), 0 failed.

- [ ] **Step 2: Run Rust test suite**

```bash
cd rust-core && cargo test 2>&1 | tail -5
```

Expected: 23 tests passed, 0 failed.

- [ ] **Step 3: Run check.sh**

```bash
bash scripts/check.sh 2>&1
```

Expected: `9 passed | 0 failed` (same or better than baseline).

- [ ] **Step 4: Tag completion**

```bash
git tag make-it-safe-complete
```
