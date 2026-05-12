# Execution Safety Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix five critical Python-layer bugs that would cause direct capital loss in live trading: silent emergency-close no-op, missing Kalshi fees, sequential fill polling race, per-market duplicate execution, and understated daily loss limits.

**Architecture:** All changes are in the Python orchestration layer (`python-core/`). No Rust or DB schema changes. Each task is independently testable — the test suite must pass after each commit. Plan B (Rust pricing accuracy) and Plan C (Python recovery) are separate and not prerequisites.

**Tech Stack:** Python 3.12, asyncio, pytest, pytest-asyncio, uv, sqlite3, unittest.mock

---

## File Map

| File | What changes |
|---|---|
| `python-core/ev_engine.py` | Add `kalshi_fee_cents` parameter to `calculate_arb_ev` |
| `python-core/detector.py` | Import `has_open_trade`; compute and pass `kalshi_fee_cents`; add per-market dedup check |
| `python-core/tracker.py` | Fix `get_daily_loss` to include open live position amounts |
| `python-core/two_leg_executor.py` | Fix `_emergency_close` signature; fix `_gather_legs` to pass tokens + run fill polls concurrently |
| `python-core/tests/test_ev_engine.py` | Add two Kalshi-fee tests |
| `python-core/tests/test_detector.py` | Add per-market dedup test; add Kalshi-fee-rejection test |
| `python-core/tests/test_tracker.py` | Add `get_daily_loss` open-position test |
| `python-core/tests/test_two_leg_executor.py` | Add emergency-close routing tests; add concurrent-poll test |

---

## Task 1: Add Kalshi fee parameter to `calculate_arb_ev`

**Files:**
- Modify: `python-core/ev_engine.py:29-55`
- Modify: `python-core/tests/test_ev_engine.py`

- [ ] **Step 1: Write the failing tests**

Add to `python-core/tests/test_ev_engine.py`:

```python
def test_arb_ev_includes_kalshi_fee():
    # combined=0.90 → gap=10¢, poly_fee=1.8¢, slippage=0.5¢, kalshi=3.5¢ → ev_net=4.2¢
    result = calculate_arb_ev(
        combined=0.90,
        taker_fee_rate=0.02,
        slippage_cents=0.5,
        kalshi_fee_cents=3.5,
    )
    assert result["ev_net_cents"] == pytest.approx(4.2, abs=1e-3)
    assert result["verdict"] == "TRADE"


def test_arb_ev_kalshi_fee_turns_trade_negative():
    # combined=0.94 → gap=6¢, poly_fee=1.88¢, slippage=0.5¢, kalshi=4.0¢ → ev_net=-0.38¢
    result = calculate_arb_ev(
        combined=0.94,
        taker_fee_rate=0.02,
        slippage_cents=0.5,
        kalshi_fee_cents=4.0,
    )
    assert result["ev_net_cents"] < 0
    assert result["verdict"] == "SKIP"


def test_arb_ev_zero_kalshi_fee_unchanged():
    # Backward compat: kalshi_fee_cents=0.0 (default) must match the old behaviour
    without = calculate_arb_ev(combined=0.92, taker_fee_rate=0.02, slippage_cents=0.5)
    with_zero = calculate_arb_ev(combined=0.92, taker_fee_rate=0.02, slippage_cents=0.5, kalshi_fee_cents=0.0)
    assert without["ev_net_cents"] == with_zero["ev_net_cents"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd python-core && uv run pytest tests/test_ev_engine.py -v -k "kalshi_fee"
```

Expected: `TypeError` — `calculate_arb_ev() got an unexpected keyword argument 'kalshi_fee_cents'`

- [ ] **Step 3: Implement the fix in `ev_engine.py`**

Replace the entire `calculate_arb_ev` function (lines 29–55):

```python
def calculate_arb_ev(
    combined: float,
    taker_fee_rate: float = 0.02,
    slippage_cents: float = 0.5,
    p_model: Optional[float] = None,
    kalshi_fee_cents: float = 0.0,
) -> dict:
    """EV for a two-leg arbitrage position.

    combined: sum of both leg prices (< 1.0 → profit opportunity)
    taker_fee_rate: applied to total combined stake (Polymarket taker fee)
    slippage_cents: expected slippage cost in cents
    kalshi_fee_cents: Kalshi per-trade fee in cents (compute from contracts × fee_per_contract)
    p_model: optional Bayesian posterior; scales ev_net by confidence (reserved for Phase 4)
    """
    gap_cents = (1.0 - combined) * 100.0
    fee_cents = taker_fee_rate * combined * 100.0
    ev_net_cents = gap_cents - fee_cents - slippage_cents - kalshi_fee_cents

    if p_model is not None:
        confidence_factor = abs(p_model - 0.5) * 2.0
        ev_net_cents *= (0.5 + 0.5 * confidence_factor)

    return {
        "ev_cents": round(gap_cents, 4),
        "ev_net_cents": round(ev_net_cents, 4),
        "verdict": "TRADE" if ev_net_cents > 0 else "SKIP",
        "p_model": p_model,
    }
```

- [ ] **Step 4: Run all ev_engine tests**

```bash
cd python-core && uv run pytest tests/test_ev_engine.py -v
```

Expected: all tests PASS (including the 3 new ones and all 5 existing ones).

- [ ] **Step 5: Commit**

```bash
git add python-core/ev_engine.py python-core/tests/test_ev_engine.py
git commit -m "feat: add kalshi_fee_cents parameter to calculate_arb_ev"
```

---

## Task 2: Wire Kalshi fees into the detector

**Files:**
- Modify: `python-core/detector.py:86-94`
- Modify: `python-core/tests/test_detector.py`

- [ ] **Step 1: Write the failing test**

Add to `python-core/tests/test_detector.py` inside `class TestCombinedPriceCheck`:

```python
def test_kalshi_fee_kills_thin_gap(self):
    # 6¢ gross gap, Kalshi fee at 0.07/contract × 20 contracts / $10 bet = 14¢/dollar = 14¢ per $1 stake
    # kalshi_price=0.50 → contracts=round(10/0.50)=20, fee=0.07*20=1.40 → fee_cents=14.0 on $10 bet
    # gap=6¢, poly_fee≈1.88¢, slippage=0.5¢, kalshi=14.0¢ → ev_net << 0
    gap = {
        **BASE_GAP,
        "polymarket_price": 0.50,
        "kalshi_price": 0.50,
        "gap_cents": 6.0,
        "confidence": "high",
    }
    detector, _ = make_detector({
        "min_bet_usdc": 10.0,
        "kalshi_fee_per_contract": 0.07,
        "ev_min_cents": 1.0,
    })
    is_valid, reason = feed_gap(detector, gap)
    assert not is_valid
    assert "EV" in reason or "ev" in reason.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd python-core && uv run pytest tests/test_detector.py -v -k "kalshi_fee"
```

Expected: FAIL — test currently passes because Kalshi fee is not computed, so the gap incorrectly clears the EV check.

- [ ] **Step 3: Update `detector.py` to compute and pass Kalshi fee**

In `python-core/detector.py`, find the block that calls `calculate_arb_ev` (lines 86–95). Replace it:

```python
        taker_fee_rate = gap.get("fee_rate", self.config.get("ev_taker_fee_rate", 0.02))
        slippage_cents = self.config.get("ev_slippage_cents", 0.5)
        ev_min_cents = self.config.get("ev_min_cents", 1.0)

        # Kalshi charges per-contract fees on top of the taker rate.
        # Only applies to cross_platform pairs (internal pairs are both Polymarket).
        kalshi_fee_cents = 0.0
        if pair_type == "cross_platform":
            bet_usdc = self.config.get("min_bet_usdc", 10.0)
            k_price = gap.get("kalshi_price", 0.5)
            fee_per_contract = self.config.get("kalshi_fee_per_contract", 0.035)
            contracts = max(1, round(bet_usdc / k_price)) if k_price > 0 else 1
            # fee in cents per dollar of total stake
            kalshi_fee_cents = (fee_per_contract * contracts / bet_usdc) * 100.0

        ev_result = calculate_arb_ev(
            combined=combined,
            taker_fee_rate=taker_fee_rate,
            slippage_cents=slippage_cents,
            p_model=gap.get("p_model"),
            kalshi_fee_cents=kalshi_fee_cents,
        )
```

- [ ] **Step 4: Run all detector tests**

```bash
cd python-core && uv run pytest tests/test_detector.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add python-core/detector.py python-core/tests/test_detector.py
git commit -m "feat: compute and apply Kalshi per-contract fee in gap detector EV check"
```

---

## Task 3: Fix per-market duplicate execution in the detector

**Files:**
- Modify: `python-core/detector.py:1-10` (imports), `detector.py:32-38` (validate start)
- Modify: `python-core/tests/test_detector.py`

- [ ] **Step 1: Write the failing test**

Add to `python-core/tests/test_detector.py`:

```python
class TestDuplicateExecution:
    def test_rejects_if_open_trade_exists_for_market(self):
        detector, conn = make_detector()
        # Insert a gap and an open trade for the same market
        cur = conn.execute(
            """INSERT INTO gaps (market_id, polymarket_price, kalshi_price, gap_cents,
               confidence, detected_at) VALUES (?,?,?,?,?,datetime('now'))""",
            ("test-market", 0.71, 0.58, 13.0, "high"),
        )
        conn.commit()
        gap_id = cur.lastrowid
        conn.execute(
            """INSERT INTO trades (gap_id, amount_usdc, status, dry_run, opened_at)
               VALUES (?, 50.0, 'open', 0, datetime('now'))""",
            (gap_id,),
        )
        conn.commit()

        is_valid, reason = detector.validate(BASE_GAP)
        assert not is_valid
        assert "open trade" in reason.lower()

    def test_allows_execution_after_trade_resolves(self):
        detector, conn = make_detector()
        cur = conn.execute(
            """INSERT INTO gaps (market_id, polymarket_price, kalshi_price, gap_cents,
               confidence, detected_at) VALUES (?,?,?,?,?,datetime('now'))""",
            ("test-market", 0.71, 0.58, 13.0, "high"),
        )
        conn.commit()
        gap_id = cur.lastrowid
        conn.execute(
            """INSERT INTO trades (gap_id, amount_usdc, status, dry_run, opened_at)
               VALUES (?, 50.0, 'resolved', 0, datetime('now'))""",
            (gap_id,),
        )
        conn.commit()

        is_valid, reason = feed_gap(detector, BASE_GAP)
        assert is_valid, f"Expected valid after resolved trade, got: {reason}"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd python-core && uv run pytest tests/test_detector.py -v -k "duplicate"
```

Expected: FAIL — detector currently does not check for open trades per market.

- [ ] **Step 3: Update `detector.py` imports**

At the top of `python-core/detector.py`, the import line currently reads:

```python
from tracker import get_daily_loss, get_open_position_count
```

Replace with:

```python
from tracker import get_daily_loss, get_open_position_count, has_open_trade
```

- [ ] **Step 4: Add per-market dedup check at the start of `validate()`**

In `python-core/detector.py`, inside `validate()`, the current first line after extracting variables is the kill-switch gate at line ~39. Insert this block BEFORE the kill-switch check:

```python
        # Check -1: Per-market dedup — never execute if we already hold an open position
        # on this market (covers both live and dry-run to prevent double-sizing).
        if has_open_trade(self.db_conn, market_id):
            return False, f"Already have open trade for {market_id} — skipping"
```

- [ ] **Step 5: Run all detector tests**

```bash
cd python-core && uv run pytest tests/test_detector.py -v
```

Expected: all tests PASS including the 2 new ones.

- [ ] **Step 6: Commit**

```bash
git add python-core/detector.py python-core/tests/test_detector.py
git commit -m "fix: add per-market open-trade dedup check to detector — prevents double-sizing"
```

---

## Task 4: Fix `get_daily_loss` to include open position exposure

**Files:**
- Modify: `python-core/tracker.py:252-257`
- Modify: `python-core/tests/test_tracker.py`

**Why:** The current implementation only counts `status='resolved'` trades. Open live positions that are currently losing are invisible. The loss limit fires too late.

- [ ] **Step 1: Write the failing test**

Add to `python-core/tests/test_tracker.py`:

```python
class TestGetDailyLoss:
    def test_counts_open_live_exposure_as_potential_loss(self, db):
        # Arrange: one resolved loss + one open live trade
        cur = db.execute(
            """INSERT INTO gaps (market_id, polymarket_price, kalshi_price,
               gap_cents, confidence, detected_at)
               VALUES ('mkt-a', 0.5, 0.5, 10.0, 'high', datetime('now'))"""
        )
        db.commit()
        gap_id = cur.lastrowid
        # Resolved loss
        db.execute(
            """INSERT INTO trades (gap_id, amount_usdc, actual_profit, status, dry_run, opened_at)
               VALUES (?, 50.0, -15.0, 'resolved', 0, datetime('now'))""",
            (gap_id,),
        )
        # Open live trade
        db.execute(
            """INSERT INTO trades (gap_id, amount_usdc, status, dry_run, opened_at)
               VALUES (?, 30.0, 'open', 0, datetime('now'))""",
            (gap_id,),
        )
        db.commit()

        loss = tracker.get_daily_loss(db)
        # 15 realized + 30 open exposure = 45
        assert loss == pytest.approx(45.0, abs=0.01)

    def test_dry_run_open_not_counted(self, db):
        cur = db.execute(
            """INSERT INTO gaps (market_id, polymarket_price, kalshi_price,
               gap_cents, confidence, detected_at)
               VALUES ('mkt-b', 0.5, 0.5, 10.0, 'high', datetime('now'))"""
        )
        db.commit()
        gap_id = cur.lastrowid
        db.execute(
            """INSERT INTO trades (gap_id, amount_usdc, status, dry_run, opened_at)
               VALUES (?, 100.0, 'open', 1, datetime('now'))""",
            (gap_id,),
        )
        db.commit()

        loss = tracker.get_daily_loss(db)
        # Dry-run positions do not count against real loss limit
        assert loss == pytest.approx(0.0, abs=0.01)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd python-core && uv run pytest tests/test_tracker.py -v -k "daily_loss"
```

Expected: `test_counts_open_live_exposure_as_potential_loss` FAIL — returns 15.0 instead of 45.0.

- [ ] **Step 3: Replace `get_daily_loss` in `tracker.py`**

Replace lines 252–257 with:

```python
def get_daily_loss(conn: sqlite3.Connection) -> float:
    """Realized losses today plus worst-case exposure of all open live positions."""
    realized = conn.execute(
        """SELECT COALESCE(SUM(actual_profit), 0) FROM trades
           WHERE opened_at > date('now') AND status='resolved'
           AND actual_profit < 0 AND dry_run=0"""
    ).fetchone()[0]
    open_exposure = conn.execute(
        """SELECT COALESCE(SUM(amount_usdc), 0) FROM trades
           WHERE opened_at > date('now') AND status='open' AND dry_run=0"""
    ).fetchone()[0]
    return abs(realized) + open_exposure
```

- [ ] **Step 4: Run all tracker tests**

```bash
cd python-core && uv run pytest tests/test_tracker.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add python-core/tracker.py python-core/tests/test_tracker.py
git commit -m "fix: daily loss limit now includes open live position exposure"
```

---

## Task 5: Fix `_emergency_close` — platform routing and correct field passing

**Files:**
- Modify: `python-core/two_leg_executor.py` — `_emergency_close`, `_gather_legs`, `_execute_cross_platform`, `_execute_internal`
- Modify: `python-core/tests/test_two_leg_executor.py`

**The bug:** `_emergency_close` derives `platform` from the order-response dict, which never contains a `"platform"` key. Both branches are dead. Close orders are never sent. Positions are silently left open.

- [ ] **Step 1: Write the failing tests**

Add to `python-core/tests/test_two_leg_executor.py`:

```python
@pytest.mark.asyncio
async def test_partial_fill_poly_closes_poly_position(config, db, cross_platform_gap):
    """When Poly fills but Kalshi fails, emergency close must call poly.close_order."""
    executor = TwoLegExecutor(config, db)

    poly_result = {"order_id": "poly_1", "status": "matched"}
    kalshi_error = ExecutorError("Kalshi rejected")

    close_mock = AsyncMock()
    executor._poly.close_order = close_mock
    executor._poly.place_order = AsyncMock(return_value=poly_result)
    executor._kalshi.place_order = AsyncMock(side_effect=kalshi_error)
    executor._poly.get_order_status = AsyncMock(return_value="matched")
    executor._kalshi.get_balance = AsyncMock(return_value=1000.0)
    executor._poly.get_balance = AsyncMock(return_value=1000.0)

    await executor.execute(cross_platform_gap)

    close_mock.assert_called_once()
    call_kwargs = close_mock.call_args
    # Must be called with the correct Polymarket token ID
    assert call_kwargs.kwargs.get("token_id") == cross_platform_gap["polymarket_token"] or \
           (call_kwargs.args and cross_platform_gap["polymarket_token"] in str(call_kwargs))


@pytest.mark.asyncio
async def test_partial_fill_kalshi_closes_kalshi_position(config, db, cross_platform_gap):
    """When Kalshi fills but Poly fails, emergency close must call kalshi.close_order."""
    executor = TwoLegExecutor(config, db)

    poly_error = ExecutorError("Poly rejected")
    kalshi_result = {"order_id": "kal_1", "status": "resting"}

    executor._poly.place_order = AsyncMock(side_effect=poly_error)
    executor._kalshi.place_order = AsyncMock(return_value=kalshi_result)
    kalshi_close_mock = AsyncMock()
    executor._kalshi.close_order = kalshi_close_mock
    executor._kalshi.get_order_status = AsyncMock(return_value="executed")
    executor._poly.get_balance = AsyncMock(return_value=1000.0)

    await executor.execute(cross_platform_gap)

    kalshi_close_mock.assert_called_once()
    call_kwargs = kalshi_close_mock.call_args
    assert call_kwargs.kwargs.get("ticker") == cross_platform_gap["kalshi_ticker"] or \
           cross_platform_gap["kalshi_ticker"] in str(call_kwargs)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd python-core && uv run pytest tests/test_two_leg_executor.py -v -k "partial_fill"
```

Expected: both tests FAIL — `close_order` is never called because `platform == ""`.

- [ ] **Step 3: Replace `_emergency_close` in `two_leg_executor.py`**

Remove the old `_emergency_close` method (lines 267–306) and replace with:

```python
    async def _emergency_close(
        self,
        platform: str,
        order_id: str,
        token_or_ticker: str,
        amount_usdc: float,
        count: Optional[int],
        price: float,
        pair_type: str,
        market_id: str,
    ) -> None:
        """Emergency-close one filled leg after the other leg failed.

        Parameters are explicit — never derived from an order-response dict,
        which does not carry platform or token metadata.
        """
        try:
            if platform == "polymarket":
                await self._poly.close_order(
                    token_id=token_or_ticker,
                    amount_usdc=amount_usdc,
                    price=price,
                    neg_risk=(pair_type == "internal"),
                )
            elif platform == "kalshi":
                await self._kalshi.close_order(
                    ticker=token_or_ticker,
                    count=count,
                )
            else:
                log.error("Emergency close: unknown platform '%s' for order %s", platform, order_id)
            status = "closed_auto"
        except Exception as e:
            log.error(
                "Emergency close FAILED for %s on %s: %s — REQUIRES MANUAL ACTION",
                order_id, platform, e,
            )
            status = "open"

        log_emergency_position(self._db, {
            "market_id": market_id,
            "platform": platform,
            "order_id": order_id,
            "side": token_or_ticker,
            "amount_usdc": amount_usdc,
        })
        if status == "closed_auto":
            self._db.execute(
                "UPDATE emergency_positions SET status='closed_auto', closed_at=datetime('now') "
                "WHERE order_id=?",
                (order_id,),
            )
            self._db.commit()
```

- [ ] **Step 4: Update `_gather_legs` signature and partial-fill branch**

Change the `_gather_legs` signature from:

```python
    async def _gather_legs(
        self,
        gap: dict,
        task_a,
        task_b,
        bet_size: float,
        poly_amount: float,
        kalshi_count: Optional[int],
    ) -> Optional[dict]:
```

to:

```python
    async def _gather_legs(
        self,
        gap: dict,
        task_a,
        task_b,
        bet_size: float,
        poly_amount: float,
        kalshi_count: Optional[int],
        poly_token: str,
        kalshi_ticker: str,
    ) -> Optional[dict]:
```

Then replace the partial-fill block at the bottom of `_gather_legs` (the `if not a_ok and not b_ok` block through the end of the method) with:

```python
        if not a_ok and not b_ok:
            log.warning(
                "Both legs failed for %s — a: %s | b: %s",
                gap["market_id"], result_a, result_b,
            )
            return None

        pair_type = gap.get("pair_type", "cross_platform")

        if a_ok and not b_ok:
            # Poly filled, Kalshi failed — emergency close Poly position
            poly_id = result_a.get("order_id", "")
            log.error(
                "PARTIAL FILL on %s — Poly filled (%s), Kalshi failed (%s) — emergency closing Poly",
                gap["market_id"], poly_id, result_b,
            )
            await self._emergency_close(
                platform="polymarket",
                order_id=poly_id,
                token_or_ticker=poly_token,
                amount_usdc=poly_amount,
                count=None,
                price=gap.get("polymarket_price", 0.5),
                pair_type=pair_type,
                market_id=gap.get("market_id", ""),
            )
            return None

        if b_ok and not a_ok:
            # Kalshi filled, Poly failed — emergency close Kalshi position
            kalshi_id = result_b.get("order_id", "")
            k_amount = (kalshi_count * gap.get("kalshi_price", 0.5)) if kalshi_count else 0.0
            log.error(
                "PARTIAL FILL on %s — Kalshi filled (%s), Poly failed (%s) — emergency closing Kalshi",
                gap["market_id"], kalshi_id, result_a,
            )
            await self._emergency_close(
                platform="kalshi",
                order_id=kalshi_id,
                token_or_ticker=kalshi_ticker,
                amount_usdc=k_amount,
                count=kalshi_count,
                price=gap.get("kalshi_price", 0.5),
                pair_type=pair_type,
                market_id=gap.get("market_id", ""),
            )
            return None

        return None  # unreachable
```

- [ ] **Step 5: Update callers of `_gather_legs` to pass new parameters**

In `_execute_cross_platform`, change the `return await self._gather_legs(...)` call (around line 158) to:

```python
        return await self._gather_legs(
            gap, poly_task, kalshi_task, bet_size=bet_size,
            poly_amount=poly_amount, kalshi_count=kalshi_count,
            poly_token=gap["polymarket_token"],
            kalshi_ticker=gap["kalshi_ticker"],
        )
```

In `_execute_internal`, change the `return await self._gather_legs(...)` call (around line 185) to:

```python
        return await self._gather_legs(
            gap, task_a, task_b, bet_size=bet_size,
            poly_amount=amount_a, kalshi_count=None,
            poly_token=gap["polymarket_token"],
            kalshi_ticker=gap["kalshi_ticker"],
        )
```

- [ ] **Step 6: Run two_leg_executor tests**

```bash
cd python-core && uv run pytest tests/test_two_leg_executor.py -v
```

Expected: all tests PASS including the 2 new partial-fill tests.

- [ ] **Step 7: Commit**

```bash
git add python-core/two_leg_executor.py python-core/tests/test_two_leg_executor.py
git commit -m "fix: emergency close now routes by explicit platform param — Poly and Kalshi positions actually closed on partial fill"
```

---

## Task 6: Fix fill polling to run concurrently

**Files:**
- Modify: `python-core/two_leg_executor.py` — `_gather_legs` fill-poll section (lines ~205-233)
- Modify: `python-core/tests/test_two_leg_executor.py`

**The bug:** Poly fill poll runs 0–30s, then Kalshi fill poll runs 0–30s. Total window with one leg exposed: up to 60 seconds. If Kalshi fills in second 2 and Poly times out at second 30, the system misidentifies the partial-fill direction.

- [ ] **Step 1: Write the failing test**

Add to `python-core/tests/test_two_leg_executor.py`:

```python
@pytest.mark.asyncio
async def test_fill_polls_run_concurrently(config, db, cross_platform_gap):
    """Both fill polls must start at the same time, not sequentially."""
    import time
    executor = TwoLegExecutor(config, db)

    poly_result = {"order_id": "poly_1"}
    kalshi_result = {"order_id": "kal_1"}

    executor._poly.place_order = AsyncMock(return_value=poly_result)
    executor._kalshi.place_order = AsyncMock(return_value=kalshi_result)
    executor._poly.get_balance = AsyncMock(return_value=1000.0)

    # Each poll takes 0.1s — concurrent = 0.1s total, sequential = 0.2s total
    async def slow_status(platform, order_id):
        await asyncio.sleep(0.1)
        return True

    executor._wait_for_fill = slow_status

    start = time.monotonic()
    await executor.execute(cross_platform_gap)
    elapsed = time.monotonic() - start

    # If sequential: elapsed >= 0.2s. If concurrent: elapsed < 0.15s.
    assert elapsed < 0.15, f"Fill polls appear sequential: took {elapsed:.3f}s"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd python-core && uv run pytest tests/test_two_leg_executor.py -v -k "concurrent"
```

Expected: FAIL — elapsed ≥ 0.2s because polls are sequential.

- [ ] **Step 3: Replace the sequential fill-poll block in `_gather_legs`**

Locate the block in `_gather_legs` that starts with `# Verify fills for legs that returned a response` (around lines 205–233). Replace the entire block with:

```python
        if not self._config.get("dry_run", True):
            poly_id = result_a.get("order_id", "") if a_ok else ""
            kalshi_id = result_b.get("order_id", "") if (b_ok and kalshi_count is not None) else ""

            async def _poll_poly() -> bool:
                if not poly_id:
                    return a_ok
                filled = await self._wait_for_fill("polymarket", poly_id)
                if not filled:
                    log.warning(
                        "Polymarket order %s did not fill in %ss — canceling",
                        poly_id, _FILL_TIMEOUT,
                    )
                    try:
                        await self._poly.cancel_order(poly_id)
                    except Exception:
                        pass
                return filled

            async def _poll_kalshi() -> bool:
                if not kalshi_id:
                    return b_ok
                filled = await self._wait_for_fill("kalshi", kalshi_id)
                if not filled:
                    log.warning(
                        "Kalshi order %s did not fill in %ss — canceling",
                        kalshi_id, _FILL_TIMEOUT,
                    )
                    try:
                        await self._kalshi.cancel_order(kalshi_id)
                    except Exception:
                        pass
                return filled

            a_ok, b_ok = await asyncio.gather(_poll_poly(), _poll_kalshi())
```

- [ ] **Step 4: Run all two_leg_executor tests**

```bash
cd python-core && uv run pytest tests/test_two_leg_executor.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add python-core/two_leg_executor.py python-core/tests/test_two_leg_executor.py
git commit -m "fix: run Poly and Kalshi fill polls concurrently — halves max exposure window from 60s to 30s"
```

---

## Task 7: Full regression check

- [ ] **Step 1: Run the complete Python test suite**

```bash
cd python-core && uv run pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all 122+ tests PASS. Zero failures.

- [ ] **Step 2: Verify Rust tests still pass**

```bash
cd rust-core && cargo test 2>&1 | tail -10
```

Expected: all Rust tests PASS (no Python changes touch Rust).

- [ ] **Step 3: Commit if any minor fixups were needed**

If Step 1 or 2 required any adjustments, commit them:

```bash
git add -p
git commit -m "fix: regression fixes from full test suite run"
```

---

## Self-Review

**Spec coverage check:**
- Issue #1 (emergency close no-op) → Task 5 ✓
- Issue #4 (sequential fill polling) → Task 6 ✓
- Issue #6 (Kalshi fees missing) → Tasks 1–2 ✓
- Issue #9 (daily loss understated) → Task 4 ✓
- Issue #17 (per-market dedup missing) → Task 3 ✓
- Issue #12 (emergency close missing token_id) → Task 5 (fixed as part of refactor) ✓

**What this plan does NOT cover (separate plans):**
- Bid/ask pricing error in Rust (Plan B)
- Kalshi orderbook BTreeMap (Plan B)
- Reconciler broken lookup (Plan C)
- compute_actual_profit ignores resolution (Plan C)
- Startup phantom gaps (Plan B)
- Rate limit handling (Plan C)
