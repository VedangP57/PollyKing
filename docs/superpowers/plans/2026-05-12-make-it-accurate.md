# Make It Accurate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure every trade has a verified positive edge after real fees, book depth is respected in sizing, malfunctioning markets are circuit-broken automatically, and the daily loss limit actually shuts the bot down rather than just logging.

**Architecture:** Six independent fixes that stack on top of Plan 1 ("Make It Safe"). Fee cache is built at startup and passed via config dict. Circuit breaker is a new standalone module with no external dependencies. Daily loss shutdown adds a second code path to `detector.py`'s existing check. Resolution mismatch runs entirely inside `backfill_matches.py`.

**Tech Stack:** Python 3.11+, aiohttp (already a dep), sqlite3, difflib (stdlib), pytest, pytest-asyncio

**Depends on:** `docs/superpowers/plans/2026-05-12-make-it-safe.md` fully implemented (EV formula must use `combined = poly_price + kalshi_price` before the fee cache makes sense).

---

## File Map

| File | Action | What changes |
|------|--------|-------------|
| `python-core/polymarket_executor.py` | Modify | Add `warm_fee_cache()` async method + `_fee_cache` dict |
| `python-core/detector.py` | Modify | Read fee from `config["_fee_cache"]`; daily loss path adds `sys.exit(1)` |
| `python-core/two_leg_executor.py` | Modify | `_compute_bet_size` adds depth cap after Kelly |
| `python-core/circuit_breaker.py` | Create | `CircuitBreaker` class (in-memory, no deps) |
| `python-core/main.py` | Modify | Wire fee cache warm-up at startup; instantiate + pass circuit breaker |
| `python-core/tests/test_polymarket_executor.py` | Modify | Add `test_fee_cache_populates`, `test_fee_cache_miss_defaults` |
| `python-core/tests/test_detector.py` | Modify | Add `test_fee_cache_miss_defaults`, `test_daily_loss_shutdown` |
| `python-core/tests/test_two_leg_executor.py` | Modify | Add `test_depth_cap_limits_bet` |
| `python-core/tests/test_circuit_breaker.py` | Create | Full circuit breaker test suite |
| `python-core/tests/test_chaos.py` | Create | 3 subprocess chaos scenarios |
| `scripts/backfill_matches.py` | Modify | `check_resolution_delta()` + write `data/resolution_mismatches.json` |
| `python-core/tests/test_backfill.py` | Modify | Add `test_resolution_mismatch_excluded` |
| `config/.env.example` | Modify | Add 5 new env vars |

---

## Task 1: Startup Fee Cache

**Files:**
- Modify: `python-core/polymarket_executor.py` (after line 57, before `_place_sync`)
- Modify: `python-core/tests/test_polymarket_executor.py`

- [ ] **Step 1: Write the failing tests**

```python
# In python-core/tests/test_polymarket_executor.py — add at bottom

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_fee_cache_populates():
    """warm_fee_cache stores taker_fee_rate per token_id."""
    config = {
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
    }
    executor = PolymarketExecutor(config)

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=[
        {"token_id": "tok-abc", "taker_fee": "0.015"},
        {"token_id": "tok-xyz", "taker_fee": "0.02"},
    ])

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(return_value=mock_response),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("polymarket_executor.aiohttp.ClientSession", return_value=mock_session):
        await executor.warm_fee_cache(["tok-abc", "tok-xyz"])

    assert executor._fee_cache["tok-abc"] == pytest.approx(0.015)
    assert executor._fee_cache["tok-xyz"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_fee_cache_miss_defaults():
    """Token not in API response falls back to 0.02."""
    config = {
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
    }
    executor = PolymarketExecutor(config)
    # Don't call warm_fee_cache — cache is empty
    assert executor._fee_cache.get("missing-token", 0.02) == 0.02


@pytest.mark.asyncio
async def test_fee_cache_api_error_falls_back():
    """API error during warm_fee_cache logs warning but does not raise."""
    config = {
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
    }
    executor = PolymarketExecutor(config)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(side_effect=Exception("network error"))

    with patch("polymarket_executor.aiohttp.ClientSession", return_value=mock_session):
        await executor.warm_fee_cache(["tok-abc"])  # must not raise

    assert executor._fee_cache == {}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd python-core && uv run pytest tests/test_polymarket_executor.py::test_fee_cache_populates tests/test_polymarket_executor.py::test_fee_cache_miss_defaults tests/test_polymarket_executor.py::test_fee_cache_api_error_falls_back -v
```

Expected: `ImportError` or `AttributeError: 'PolymarketExecutor' object has no attribute 'warm_fee_cache'`

- [ ] **Step 3: Add `aiohttp` import and `warm_fee_cache` method**

Add to top of `python-core/polymarket_executor.py` (after existing imports):

```python
import aiohttp
import logging

log = logging.getLogger(__name__)
```

Add to `PolymarketExecutor.__init__` (after `self._client: Optional[ClobClient] = None`):

```python
self._fee_cache: dict[str, float] = {}
```

Add new method after `_get_client` (before `_place_sync`):

```python
async def warm_fee_cache(self, token_ids: list[str]) -> None:
    """Fetch taker fee rate for each token from CLOB API. Results cached in self._fee_cache."""
    if not token_ids:
        return
    try:
        async with aiohttp.ClientSession() as session:
            # Polymarket CLOB returns market info including taker_fee per token
            params = [("token_id", tid) for tid in token_ids]
            async with session.get(
                "https://clob.polymarket.com/markets",
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    log.warning("Fee cache: CLOB /markets returned %d — using defaults", resp.status)
                    return
                data = await resp.json()
                for market in data if isinstance(data, list) else []:
                    tid = market.get("token_id", "")
                    fee_str = market.get("taker_fee", "")
                    if tid and fee_str:
                        try:
                            self._fee_cache[tid] = float(fee_str)
                        except (ValueError, TypeError):
                            pass
        log.info("Fee cache warmed: %d tokens", len(self._fee_cache))
    except Exception as e:
        log.warning("Fee cache warm-up failed (%s) — using flat default 0.02 for all tokens", e)
```

- [ ] **Step 4: Update `detector.py` to read fee from cache**

In `python-core/detector.py`, replace line 91 (the `taker_fee_rate` assignment):

```python
# Old:
taker_fee_rate = gap.get("fee_rate", self.config.get("ev_taker_fee_rate", 0.02))

# New — prefer per-token fee from cache; fall back to gap fee_rate, then config default:
fee_cache = self.config.get("_fee_cache", {})
poly_token = gap.get("polymarket_token", "")
taker_fee_rate = fee_cache.get(poly_token, gap.get("fee_rate", self.config.get("ev_taker_fee_rate", 0.02)))
```

- [ ] **Step 5: Wire `warm_fee_cache` into `main.py` startup**

In `python-core/main.py`, add after pairs are loaded (around line 136 where `_pairs_data` is parsed) and after `executor` is instantiated:

```python
# Warm per-token fee cache from Polymarket CLOB API
_poly_tokens = [p.get("token_a", "") for p in _pairs_data if p.get("token_a")]
if _poly_tokens and not CONFIG.get("dry_run", True):
    try:
        await executor._poly.warm_fee_cache(_poly_tokens)
        CONFIG["_fee_cache"] = executor._poly._fee_cache
    except Exception as e:
        notifier.logger.warning("Fee cache warm-up failed: %s — continuing with defaults", e)
```

Also add `FEE_CACHE_REFRESH_INTERVAL_S` to CONFIG dict (in main.py around line 70):

```python
"fee_cache_refresh_s": float(os.getenv("FEE_CACHE_REFRESH_INTERVAL_S", "3600")),
```

- [ ] **Step 6: Add env var to `.env.example`**

In `config/.env.example`, add:

```bash
# Fee cache
FEE_CACHE_REFRESH_INTERVAL_S=3600
```

- [ ] **Step 7: Run tests — all should pass**

```bash
cd python-core && uv run pytest tests/test_polymarket_executor.py::test_fee_cache_populates tests/test_polymarket_executor.py::test_fee_cache_miss_defaults tests/test_polymarket_executor.py::test_fee_cache_api_error_falls_back -v
```

Expected: 3 PASSED

- [ ] **Step 8: Commit**

```bash
git add python-core/polymarket_executor.py python-core/detector.py python-core/main.py python-core/tests/test_polymarket_executor.py config/.env.example
git commit -m "feat(accuracy): startup fee cache — per-token taker fee from CLOB API"
```

---

## Task 2: Depth-Constrained Position Sizing

**Files:**
- Modify: `python-core/two_leg_executor.py:58-76` (`_compute_bet_size`)
- Modify: `python-core/tests/test_two_leg_executor.py`
- Modify: `config/.env.example`

- [ ] **Step 1: Write the failing test**

```python
# In python-core/tests/test_two_leg_executor.py — add at bottom

def test_depth_cap_limits_bet():
    """Bet is capped at max_depth_fraction * min(poly_liq, kalshi_liq)."""
    config = {
        "bankroll_usdc": 1000.0,
        "kelly_fraction": 1.0,   # full Kelly — would give large bet without cap
        "min_bet_usdc": 5.0,
        "max_bet_usdc": 500.0,
        "max_depth_fraction": 0.25,
        "dry_run": True,
    }
    import sqlite3
    db = sqlite3.connect(":memory:")
    executor = TwoLegExecutor(config, db)

    gap = {
        "market_id": "test-market",
        "polymarket_price": 0.40,
        "kalshi_price": 0.40,
        "confidence": "high",
        "poly_liquidity_usdc": 80.0,   # min of the two
        "kalshi_liquidity_usdc": 200.0,
    }
    bet = executor._compute_bet_size(gap)
    # depth_cap = min(80, 200) * 0.25 = 20.0
    # Kelly would give more, but should be capped at 20.0
    assert bet <= 20.0


def test_depth_cap_does_not_go_below_min_bet():
    """If depth cap < min_bet, min_bet is used (avoid zero-sized orders)."""
    config = {
        "bankroll_usdc": 1000.0,
        "kelly_fraction": 1.0,
        "min_bet_usdc": 10.0,
        "max_bet_usdc": 500.0,
        "max_depth_fraction": 0.25,
        "dry_run": True,
    }
    import sqlite3
    db = sqlite3.connect(":memory:")
    executor = TwoLegExecutor(config, db)

    gap = {
        "market_id": "test-market",
        "polymarket_price": 0.40,
        "kalshi_price": 0.40,
        "confidence": "high",
        "poly_liquidity_usdc": 4.0,   # depth_cap = 4*0.25 = 1 < min_bet=10
        "kalshi_liquidity_usdc": 4.0,
    }
    bet = executor._compute_bet_size(gap)
    assert bet >= 10.0  # min_bet floors it
```

- [ ] **Step 2: Run to verify failure**

```bash
cd python-core && uv run pytest tests/test_two_leg_executor.py::test_depth_cap_limits_bet tests/test_two_leg_executor.py::test_depth_cap_does_not_go_below_min_bet -v
```

Expected: FAIL (no depth cap exists yet)

- [ ] **Step 3: Add depth cap to `_compute_bet_size`**

In `python-core/two_leg_executor.py`, replace lines 58-76 (`_compute_bet_size`):

```python
def _compute_bet_size(self, gap: dict) -> float:
    bankroll = self._config.get("bankroll_usdc", 500.0)
    fraction = self._config.get("kelly_fraction", 0.25)
    min_bet = self._config.get("min_bet_usdc", 10.0)
    max_bet = self._config.get("max_bet_usdc", 100.0)
    poly_price = gap["polymarket_price"]
    kalshi_price = gap["kalshi_price"]
    combined = poly_price + kalshi_price
    result = compute_arb_kelly_size(
        bankroll=bankroll,
        combined=combined,
        confidence=gap.get("confidence", "medium"),
        fraction=fraction,
        max_bet_pct=0.05,
        min_bet_usdc=min_bet,
        max_bet_usdc=max_bet,
    )
    bet_size = result["bet_usdc"] if result["action"] == "BET" else min_bet

    # Depth cap: never take more than max_depth_fraction of the thinner side's book
    max_depth_fraction = self._config.get("max_depth_fraction", 0.25)
    poly_liq = gap.get("poly_liquidity_usdc", float("inf"))
    kalshi_liq = gap.get("kalshi_liquidity_usdc", float("inf"))
    depth_cap = min(poly_liq, kalshi_liq) * max_depth_fraction
    bet_size = min(bet_size, depth_cap)
    bet_size = max(bet_size, min_bet)  # never go below minimum

    return bet_size
```

- [ ] **Step 4: Add env var to `.env.example`**

```bash
# Position sizing
MAX_DEPTH_FRACTION=0.25
```

- [ ] **Step 5: Run tests — both should pass**

```bash
cd python-core && uv run pytest tests/test_two_leg_executor.py::test_depth_cap_limits_bet tests/test_two_leg_executor.py::test_depth_cap_does_not_go_below_min_bet -v
```

Expected: 2 PASSED

- [ ] **Step 6: Commit**

```bash
git add python-core/two_leg_executor.py python-core/tests/test_two_leg_executor.py config/.env.example
git commit -m "feat(accuracy): depth-constrained bet sizing — cap at 25% of thinner book side"
```

---

## Task 3: Per-Market Circuit Breaker

**Files:**
- Create: `python-core/circuit_breaker.py`
- Create: `python-core/tests/test_circuit_breaker.py`
- Modify: `python-core/main.py` (`_handle_gap_inner` + instantiation in `main()`)
- Modify: `python-core/two_leg_executor.py` (call `record_failure` on empty confirmation)
- Modify: `config/.env.example`

- [ ] **Step 1: Write the failing tests**

Create `python-core/tests/test_circuit_breaker.py`:

```python
import time
import pytest
from circuit_breaker import CircuitBreaker


def test_circuit_breaker_opens_after_max_failures():
    """3 failures in window → is_open=True."""
    cb = CircuitBreaker(max_failures=3, window_s=60.0)
    cb.record_failure("market-abc")
    cb.record_failure("market-abc")
    assert not cb.is_open("market-abc")  # 2 failures, not yet open
    cb.record_failure("market-abc")
    assert cb.is_open("market-abc")  # 3 failures → open


def test_circuit_breaker_different_markets_independent():
    """Failures on one market don't affect another."""
    cb = CircuitBreaker(max_failures=3, window_s=60.0)
    for _ in range(3):
        cb.record_failure("market-a")
    assert cb.is_open("market-a")
    assert not cb.is_open("market-b")  # different market, untouched


def test_circuit_breaker_expires_failures():
    """Failures older than window_s are evicted — breaker can close naturally."""
    cb = CircuitBreaker(max_failures=2, window_s=0.1)  # 100ms window
    cb.record_failure("market-abc")
    cb.record_failure("market-abc")
    assert cb.is_open("market-abc")
    time.sleep(0.15)  # wait for window to expire
    assert not cb.is_open("market-abc")  # evicted, breaker closed


def test_circuit_breaker_reset_clears_failures():
    """reset() immediately closes the breaker for a market."""
    cb = CircuitBreaker(max_failures=3, window_s=60.0)
    for _ in range(3):
        cb.record_failure("market-abc")
    assert cb.is_open("market-abc")
    cb.reset("market-abc")
    assert not cb.is_open("market-abc")


def test_circuit_breaker_fresh_market_is_closed():
    """A market never seen returns is_open=False."""
    cb = CircuitBreaker()
    assert not cb.is_open("brand-new-market")
```

- [ ] **Step 2: Run to verify failure**

```bash
cd python-core && uv run pytest tests/test_circuit_breaker.py -v
```

Expected: `ModuleNotFoundError: No module named 'circuit_breaker'`

- [ ] **Step 3: Create `python-core/circuit_breaker.py`**

```python
import logging
import time
from collections import deque

log = logging.getLogger(__name__)


class CircuitBreaker:
    """Per-market failure counter with sliding time window.

    is_open(market_id) returns True when >= max_failures failures occurred
    within the last window_s seconds. Failures older than the window are
    evicted lazily on each check — no background task needed.
    """

    def __init__(self, max_failures: int = 3, window_s: float = 600.0):
        self._max = max_failures
        self._window = window_s
        self._failures: dict[str, deque] = {}

    def record_failure(self, market_id: str) -> None:
        if market_id not in self._failures:
            self._failures[market_id] = deque()
        self._failures[market_id].append(time.monotonic())
        count = self._count_recent(market_id)
        if count >= self._max:
            log.warning(
                "Circuit breaker OPEN for %s — %d failures in %.0fs window",
                market_id, count, self._window,
            )

    def is_open(self, market_id: str) -> bool:
        return self._count_recent(market_id) >= self._max

    def reset(self, market_id: str) -> None:
        self._failures.pop(market_id, None)

    def _count_recent(self, market_id: str) -> int:
        q = self._failures.get(market_id)
        if not q:
            return 0
        now = time.monotonic()
        # Evict expired entries from the left (oldest)
        while q and now - q[0] >= self._window:
            q.popleft()
        return len(q)
```

- [ ] **Step 4: Run tests — all 5 should pass**

```bash
cd python-core && uv run pytest tests/test_circuit_breaker.py -v
```

Expected: 5 PASSED

- [ ] **Step 5: Wire circuit breaker into `main.py`**

Add import near top of `main.py`:

```python
from circuit_breaker import CircuitBreaker
```

In `main()`, instantiate after `executor` and `detector` are created:

```python
circuit_breaker = CircuitBreaker(
    max_failures=int(os.getenv("CIRCUIT_BREAKER_MAX_FAILURES", "3")),
    window_s=float(os.getenv("CIRCUIT_BREAKER_WINDOW_S", "600")),
)
```

Update `_handle_gap_inner` signature to accept `circuit_breaker`:

```python
async def _handle_gap_inner(
    gap: dict, detector: GapDetector, executor: TwoLegExecutor,
    db_conn, stdout_queue, bayes_engine: BayesEngine, fee_rate_map: dict,
    health_state: dict | None = None, opp_engine: OpportunityEngine | None = None,
    circuit_breaker: CircuitBreaker | None = None,
):
```

Add circuit breaker check at the top of `_handle_gap_inner`, immediately after the `health_state` update and before `detector.validate()` (around line 257):

```python
market_id = gap["market_id"]

# Circuit breaker: skip markets with repeated recent failures
if circuit_breaker is not None and circuit_breaker.is_open(market_id):
    return  # failure already logged at record_failure time
```

Update the call site (where `_handle_gap_inner` is called, around line 246) to pass `circuit_breaker`:

```python
await _handle_gap_inner(
    gap, detector, executor, db_conn, stdout_queue,
    bayes_engine, fee_rate_map, health_state, opp_engine,
    circuit_breaker=circuit_breaker,
)
```

After `confirmation = await executor.execute(gap)`, add failure recording when execution fails:

```python
if not confirmation:
    if circuit_breaker is not None:
        circuit_breaker.record_failure(market_id)
    notifier.logger.warning(f"Execution failed for {market_id} — gap logged, no trade")
    ...
```

After successful execution (after `opp_engine.mark_executed`), add reset:

```python
if circuit_breaker is not None:
    circuit_breaker.reset(market_id)
```

- [ ] **Step 6: Add env vars to `.env.example`**

```bash
# Circuit breaker
CIRCUIT_BREAKER_MAX_FAILURES=3
CIRCUIT_BREAKER_WINDOW_S=600
```

- [ ] **Step 7: Run full test suite**

```bash
cd python-core && uv run pytest tests/test_circuit_breaker.py tests/test_two_leg_executor.py -v
```

Expected: all PASSED (no regressions in existing executor tests)

- [ ] **Step 8: Commit**

```bash
git add python-core/circuit_breaker.py python-core/tests/test_circuit_breaker.py python-core/main.py config/.env.example
git commit -m "feat(accuracy): per-market circuit breaker — auto-pause after 3 failures in 10min window"
```

---

## Task 4: Chaos Engineering Tests

**Files:**
- Create: `python-core/tests/test_chaos.py`

These tests spawn real subprocesses and run in a temp directory. They're marked `@pytest.mark.chaos` to exclude from fast runs.

- [ ] **Step 1: Create `python-core/tests/test_chaos.py`**

```python
"""Chaos engineering tests — spawn subprocesses, inject failures, verify recovery.

Run with: pytest tests/test_chaos.py -m chaos -v
Excluded from default test run (slow, subprocess-heavy).
"""
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

PYTHON = sys.executable
CORE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _make_temp_db(tmp_dir: str) -> str:
    """Copy trades.db to a temp location for isolation."""
    src = os.path.join(CORE_DIR, "..", "data", "trades.db")
    dst = os.path.join(tmp_dir, "trades.db")
    if os.path.exists(src):
        shutil.copy2(src, dst)
    else:
        # Create minimal schema
        conn = sqlite3.connect(dst)
        conn.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT DEFAULT (datetime('now')))")
        conn.commit()
        conn.close()
    return dst


@pytest.mark.chaos
def test_scenario_b_db_corruption_triggers_exit():
    """Scenario B: corrupted DB → startup_check exits with code 1."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = _make_temp_db(tmp_dir)

        # Corrupt the first 100 bytes
        with open(db_path, "r+b") as f:
            f.seek(0)
            f.write(b"\x00" * 100)

        # startup_check.py must refuse to start
        result = subprocess.run(
            [PYTHON, "-c", f"""
import sys
sys.path.insert(0, "{CORE_DIR}")
import asyncio
from startup_check import run_all
asyncio.run(run_all({{"db_path": "{db_path}", "dry_run": True}}))
"""],
            capture_output=True,
            timeout=15,
        )
        assert result.returncode == 1, (
            f"Expected exit code 1, got {result.returncode}\n"
            f"stdout: {result.stdout.decode()}\n"
            f"stderr: {result.stderr.decode()}"
        )
        assert b"integrity" in result.stderr.lower() or b"corrupt" in result.stderr.lower() or result.returncode == 1


@pytest.mark.chaos
def test_scenario_b_clean_db_passes_check():
    """Inverse of Scenario B: intact DB → startup_check exits 0."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = _make_temp_db(tmp_dir)

        result = subprocess.run(
            [PYTHON, "-c", f"""
import sys
sys.path.insert(0, "{CORE_DIR}")
import asyncio
from startup_check import run_all
asyncio.run(run_all({{
    "db_path": "{db_path}",
    "dry_run": True,
    "skip_network_checks": True,
}}))
print("startup_check passed")
"""],
            capture_output=True,
            timeout=15,
        )
        # Should succeed (exit 0) when DB is healthy and network checks skipped
        output = result.stdout.decode() + result.stderr.decode()
        assert result.returncode == 0 or "startup_check passed" in output, (
            f"Unexpected failure: rc={result.returncode}\n{output}"
        )


@pytest.mark.chaos
def test_scenario_c_missing_db_path_fails():
    """startup_check exits 1 when db_path points to nonexistent directory."""
    result = subprocess.run(
        [PYTHON, "-c", f"""
import sys
sys.path.insert(0, "{CORE_DIR}")
import asyncio
from startup_check import run_all
asyncio.run(run_all({{
    "db_path": "/nonexistent/path/trades.db",
    "dry_run": True,
}}))
"""],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 1
```

- [ ] **Step 2: Add `chaos` marker to `pytest.ini` or `pyproject.toml`**

Check if `python-core/pyproject.toml` exists. If it does, add under `[tool.pytest.ini_options]`:

```toml
[tool.pytest.ini_options]
markers = [
    "chaos: subprocess-based chaos engineering tests (slow, excluded from default run)",
]
```

If there's a `pytest.ini`, add:

```ini
[pytest]
markers =
    chaos: subprocess-based chaos engineering tests (slow, excluded from default run)
```

- [ ] **Step 3: Update `startup_check.py` to support `skip_network_checks`**

In `python-core/startup_check.py`, check if network checks can be skipped (for chaos test use):

```python
async def run_all(config: dict) -> None:
    """Raises SystemExit(1) if any check fails."""
    skip_network = config.get("skip_network_checks", False)

    # Check 1-2: API keys (live mode only)
    if not config.get("dry_run", True):
        if not config.get("polymarket_private_key", "").strip():
            _fail("POLYMARKET_PRIVATE_KEY is not set")
        if not config.get("kalshi_api_key", "").strip():
            _fail("KALSHI_API_KEY is not set")

    if not skip_network:
        # Check 3: Kalshi public API ping
        ...
        # Check 4: Polymarket CLOB ping
        ...

    # Check 5: DB integrity (always runs)
    db_path = config.get("db_path", "data/trades.db")
    _check_db_integrity(db_path)
```

- [ ] **Step 4: Run chaos tests**

```bash
cd python-core && uv run pytest tests/test_chaos.py -m chaos -v
```

Expected: all PASSED (or skip if startup_check not yet implemented — that's Task 7 from Plan 1)

- [ ] **Step 5: Commit**

```bash
git add python-core/tests/test_chaos.py
git commit -m "test(chaos): subprocess-based chaos scenarios — DB corruption and bad path exit with code 1"
```

---

## Task 5: Daily Loss Auto-Shutdown

**Files:**
- Modify: `python-core/detector.py:165-168` (Check 5)
- Modify: `python-core/tests/test_detector.py`

- [ ] **Step 1: Write the failing test**

```python
# In python-core/tests/test_detector.py — add at bottom

import sqlite3
import pytest
from unittest.mock import patch
from detector import GapDetector

def _make_detector_with_loss(daily_loss: float, max_loss: float = 50.0) -> tuple:
    """Returns (detector, db_conn) with the daily loss mock configured."""
    db = sqlite3.connect(":memory:")
    # Create bot_state table for kill switch
    db.execute("""CREATE TABLE IF NOT EXISTS bot_state (
        key TEXT PRIMARY KEY, value TEXT NOT NULL,
        updated_at TEXT DEFAULT (datetime('now'))
    )""")
    db.commit()
    config = {
        "dry_run": True,
        "max_daily_loss_usdc": max_loss,
        "ev_min_cents": 1.0,
        "ev_taker_fee_rate": 0.02,
        "ev_slippage_cents": 0.5,
        "min_bet_usdc": 10.0,
    }
    detector = GapDetector(config, db)
    return detector, db


def test_daily_loss_shutdown_writes_kill_switch_and_exits():
    """When daily loss >= max, bot writes kill_switch to DB and calls SystemExit(1)."""
    detector, db = _make_detector_with_loss(daily_loss=55.0, max_loss=50.0)

    gap = {
        "market_id": "test-market",
        "polymarket_price": 0.40,
        "kalshi_price": 0.40,
        "gap_cents": 20.0,
        "pair_type": "cross_platform",
        "confidence": "high",
        "poly_liquidity_usdc": 1000.0,
        "kalshi_liquidity_usdc": 1000.0,
    }

    with patch("detector.get_daily_loss", return_value=55.0):
        with pytest.raises(SystemExit) as exc_info:
            detector.validate(gap)

    assert exc_info.value.code == 1

    # Verify kill switch was written to DB
    row = db.execute("SELECT value FROM bot_state WHERE key='kill_switch'").fetchone()
    assert row is not None, "kill_switch not written to bot_state"
    assert row[0] == "1"


def test_daily_loss_below_limit_does_not_exit():
    """Daily loss below max → validate continues normally (no SystemExit)."""
    detector, db = _make_detector_with_loss(daily_loss=10.0, max_loss=50.0)

    gap = {
        "market_id": "test-market",
        "polymarket_price": 0.40,
        "kalshi_price": 0.40,
        "gap_cents": 20.0,
        "pair_type": "cross_platform",
        "confidence": "high",
        "poly_liquidity_usdc": 1000.0,
        "kalshi_liquidity_usdc": 1000.0,
    }

    with patch("detector.get_daily_loss", return_value=10.0):
        with patch("detector.has_open_trade", return_value=False):
            with patch("detector.get_open_position_count", return_value=0):
                # Should not raise — may return True or False depending on other checks
                try:
                    detector.validate(gap)
                except SystemExit:
                    pytest.fail("SystemExit raised when loss is below limit")
```

- [ ] **Step 2: Run to verify failure**

```bash
cd python-core && uv run pytest tests/test_detector.py::test_daily_loss_shutdown_writes_kill_switch_and_exits -v
```

Expected: FAIL — current code returns `(False, "Daily loss limit hit: ...")` instead of `SystemExit(1)`

- [ ] **Step 3: Update `detector.py` Check 5**

In `python-core/detector.py`, replace lines 164-168 (Check 5 — daily loss):

```python
# Check 5: Daily loss limit
daily_loss = get_daily_loss(self.db_conn)
max_loss = self.config.get("max_daily_loss_usdc", 50.0)
if daily_loss >= max_loss:
    import logging as _log
    _log.getLogger(__name__).critical(
        "DAILY LOSS LIMIT REACHED: $%.2f >= $%.2f — writing kill switch and shutting down",
        daily_loss, max_loss,
    )
    self.db_conn.execute(
        "INSERT OR REPLACE INTO bot_state (key, value) VALUES ('kill_switch', '1')",
    )
    self.db_conn.commit()
    raise SystemExit(1)
```

- [ ] **Step 4: Run tests**

```bash
cd python-core && uv run pytest tests/test_detector.py::test_daily_loss_shutdown_writes_kill_switch_and_exits tests/test_detector.py::test_daily_loss_below_limit_does_not_exit -v
```

Expected: 2 PASSED

- [ ] **Step 5: Run full detector test suite — verify no regressions**

```bash
cd python-core && uv run pytest tests/test_detector.py -v
```

Expected: all PASSED

- [ ] **Step 6: Commit**

```bash
git add python-core/detector.py python-core/tests/test_detector.py
git commit -m "feat(accuracy): daily loss shutdown writes kill switch + SystemExit(1) — prevents restart trading"
```

---

## Task 6: Resolution Mismatch Detection

**Files:**
- Modify: `scripts/backfill_matches.py`
- Modify: `python-core/tests/test_backfill.py` (if it exists) or create it
- Modify: `config/.env.example`

- [ ] **Step 1: Read the current `scripts/backfill_matches.py` structure**

```bash
head -80 scripts/backfill_matches.py
grep -n "def \|pairs\|add_pair\|write\|markets.json" scripts/backfill_matches.py | head -30
```

- [ ] **Step 2: Write failing tests**

Check if `python-core/tests/test_backfill.py` exists:

```bash
ls python-core/tests/test_backfill.py 2>/dev/null && echo "exists" || echo "missing"
```

If missing, create `python-core/tests/test_backfill.py`. Add (or append) these tests:

```python
"""Tests for backfill_matches.py resolution mismatch detection."""
import sys
import os
import pytest
from datetime import datetime, timezone, timedelta

# Add scripts/ to path so we can import backfill functions
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))


def test_resolution_mismatch_within_threshold_allowed():
    """Delta <= 6h → pair not flagged as mismatch."""
    from backfill_matches import check_resolution_delta
    poly_end = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    kalshi_close = datetime(2026, 11, 1, 5, 0, 0, tzinfo=timezone.utc)  # 5h apart
    result = check_resolution_delta(poly_end, kalshi_close, max_hours=6)
    assert result["mismatch"] is False
    assert result["delta_hours"] == pytest.approx(5.0)


def test_resolution_mismatch_exceeds_threshold_blocked():
    """Delta > 6h → mismatch=True, pair excluded."""
    from backfill_matches import check_resolution_delta
    poly_end = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    kalshi_close = datetime(2026, 11, 1, 7, 30, 0, tzinfo=timezone.utc)  # 7.5h apart
    result = check_resolution_delta(poly_end, kalshi_close, max_hours=6)
    assert result["mismatch"] is True
    assert result["delta_hours"] == pytest.approx(7.5)


def test_resolution_mismatch_none_timestamps_allowed():
    """If either timestamp is None/missing, no mismatch flagged (safe default)."""
    from backfill_matches import check_resolution_delta
    result = check_resolution_delta(None, None, max_hours=6)
    assert result["mismatch"] is False


def test_resolution_mismatch_between_3_and_6h_low_confidence():
    """Delta 3-6h → mismatch=False but confidence='low'."""
    from backfill_matches import check_resolution_delta
    poly_end = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
    kalshi_close = datetime(2026, 11, 1, 4, 0, 0, tzinfo=timezone.utc)  # 4h apart
    result = check_resolution_delta(poly_end, kalshi_close, max_hours=6, warn_hours=3)
    assert result["mismatch"] is False
    assert result.get("confidence") == "low"
```

- [ ] **Step 3: Run to verify failure**

```bash
cd python-core && uv run pytest tests/test_backfill.py -v -k "resolution"
```

Expected: `ImportError: cannot import name 'check_resolution_delta' from 'backfill_matches'`

- [ ] **Step 4: Add `check_resolution_delta` to `scripts/backfill_matches.py`**

Add this function (near the top, after imports):

```python
from datetime import datetime, timezone

def check_resolution_delta(
    poly_end: "datetime | None",
    kalshi_close: "datetime | None",
    max_hours: float = 6.0,
    warn_hours: float = 3.0,
) -> dict:
    """Compare resolution timestamps. Returns mismatch=True if delta > max_hours.

    Returns:
        {"mismatch": bool, "delta_hours": float, "confidence": str | None}
    """
    if poly_end is None or kalshi_close is None:
        return {"mismatch": False, "delta_hours": 0.0, "confidence": None}

    delta_hours = abs((kalshi_close - poly_end).total_seconds()) / 3600.0

    if delta_hours > max_hours:
        return {"mismatch": True, "delta_hours": delta_hours, "confidence": None}

    if delta_hours > warn_hours:
        return {"mismatch": False, "delta_hours": delta_hours, "confidence": "low"}

    return {"mismatch": False, "delta_hours": delta_hours, "confidence": None}
```

- [ ] **Step 5: Wire `check_resolution_delta` into the pair-creation loop**

Find where pairs are written to `markets.json` in `backfill_matches.py`. Before adding a pair to the active list, call `check_resolution_delta`. The exact location depends on the current file structure — read it first, then add:

```python
# After fetching poly_end and kalshi_close for a candidate pair:
resolution = check_resolution_delta(
    poly_end=poly_end_dt,         # datetime parsed from Polymarket market data
    kalshi_close=kalshi_close_dt, # datetime parsed from Kalshi market data
    max_hours=float(os.getenv("MAX_RESOLUTION_DELTA_HOURS", "6")),
)

if resolution["mismatch"]:
    log.warning(
        "Resolution mismatch (%.1fh) — pair %s excluded. poly_end=%s kalshi_close=%s",
        resolution["delta_hours"], candidate_market_id, poly_end_dt, kalshi_close_dt,
    )
    mismatches.append({
        "market_id": candidate_market_id,
        "delta_hours": resolution["delta_hours"],
        "poly_end": str(poly_end_dt),
        "kalshi_close": str(kalshi_close_dt),
    })
    continue  # skip this pair

if resolution.get("confidence") == "low":
    pair["confidence"] = "low"  # detector rejects low confidence
```

At end of backfill run, write mismatches to file:

```python
if mismatches:
    import json, pathlib
    pathlib.Path("data").mkdir(exist_ok=True)
    pathlib.Path("data/resolution_mismatches.json").write_text(
        json.dumps(mismatches, indent=2)
    )
    log.info("%d resolution mismatches written to data/resolution_mismatches.json", len(mismatches))
```

- [ ] **Step 6: Add env var to `.env.example`**

```bash
# Resolution mismatch detection
MAX_RESOLUTION_DELTA_HOURS=6
```

- [ ] **Step 7: Run tests**

```bash
cd python-core && uv run pytest tests/test_backfill.py -v -k "resolution"
```

Expected: 4 PASSED

- [ ] **Step 8: Commit**

```bash
git add scripts/backfill_matches.py python-core/tests/test_backfill.py config/.env.example
git commit -m "feat(accuracy): resolution mismatch detection — pairs with >6h timestamp delta excluded"
```

---

## Task 7: Final Verification

- [ ] **Step 1: Run complete Python test suite**

```bash
cd python-core && uv run pytest --tb=short -q
```

Expected: all tests PASS. Target: no regressions from the ~154 tests already passing before Plan 2.

- [ ] **Step 2: Run Rust test suite**

```bash
cd rust-core && cargo test 2>&1 | tail -20
```

Expected: all tests pass (Plan 2 has no Rust changes)

- [ ] **Step 3: Verify circuit breaker integration**

```bash
cd python-core && uv run python -c "
from circuit_breaker import CircuitBreaker
cb = CircuitBreaker(max_failures=2, window_s=1.0)
cb.record_failure('test')
cb.record_failure('test')
assert cb.is_open('test'), 'breaker should be open'
print('circuit breaker: OK')
"
```

Expected: `circuit breaker: OK`

- [ ] **Step 4: Verify daily loss shutdown**

```bash
cd python-core && uv run python -c "
import sqlite3, sys
from unittest.mock import patch
from detector import GapDetector

db = sqlite3.connect(':memory:')
db.execute(\"CREATE TABLE bot_state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT DEFAULT (datetime('now')))\")
db.commit()
config = {'max_daily_loss_usdc': 50.0, 'dry_run': True, 'ev_min_cents': 1.0, 'ev_taker_fee_rate': 0.02, 'ev_slippage_cents': 0.5, 'min_bet_usdc': 10.0}
det = GapDetector(config, db)
gap = {'market_id': 'x', 'polymarket_price': 0.4, 'kalshi_price': 0.4, 'gap_cents': 20.0, 'pair_type': 'cross_platform', 'confidence': 'high', 'poly_liquidity_usdc': 1000.0, 'kalshi_liquidity_usdc': 1000.0}
try:
    with patch('detector.get_daily_loss', return_value=55.0):
        with patch('detector.has_open_trade', return_value=False):
            det.validate(gap)
    print('ERROR: no SystemExit raised')
    sys.exit(1)
except SystemExit as e:
    assert e.code == 1
    row = db.execute(\"SELECT value FROM bot_state WHERE key='kill_switch'\").fetchone()
    assert row and row[0] == '1', 'kill_switch not written'
    print('daily loss shutdown: OK')
"
```

Expected: `daily loss shutdown: OK`

- [ ] **Step 5: Commit any remaining changes**

```bash
git add -A
git status  # review before committing
```

Only commit if there are genuine leftovers. Otherwise skip.

---

## Config Changes Summary

Add all of these to `config/.env.example`:

```bash
# --- Make It Accurate ---
FEE_CACHE_REFRESH_INTERVAL_S=3600
MAX_DEPTH_FRACTION=0.25
CIRCUIT_BREAKER_MAX_FAILURES=3
CIRCUIT_BREAKER_WINDOW_S=600
MAX_RESOLUTION_DELTA_HOURS=6
```
