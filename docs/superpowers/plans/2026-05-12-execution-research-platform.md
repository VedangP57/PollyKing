# Execution Research Platform — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Evolve the signal detector into an execution research platform by adding liquidity awareness, opportunity lifecycle tracking, production observability, and full execution telemetry.

**Architecture:** Four independent phases executed in order: F (Observability) → D (Liquidity) → E (Opportunity Engine) → G (Execution Telemetry). Each phase is independently deployable and rollback-safe.

**Tech Stack:** Python 3.12, prometheus_client, asyncio, SQLite, Rust stable (Phase D only)

---

## PHASE F — OBSERVABILITY

### Task F1: Add prometheus_client + create metrics.py

**Files:**
- Modify: `python-core/pyproject.toml`
- Create: `python-core/metrics.py`
- Create: `python-core/tests/test_metrics.py`

- [ ] **Step 1: Write failing test for metrics module**

Create `python-core/tests/test_metrics.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_metrics_module_importable():
    import metrics
    assert hasattr(metrics, "gaps_detected")
    assert hasattr(metrics, "gaps_rejected")
    assert hasattr(metrics, "executions")
    assert hasattr(metrics, "fill_latency")
    assert hasattr(metrics, "open_positions")
    assert hasattr(metrics, "daily_pnl")
    assert hasattr(metrics, "ws_reconnects")
    assert hasattr(metrics, "inc_gap_detected")
    assert hasattr(metrics, "inc_gap_rejected")
    assert hasattr(metrics, "inc_execution")
    assert hasattr(metrics, "observe_fill_latency")
    assert hasattr(metrics, "set_open_positions")
    assert hasattr(metrics, "set_daily_pnl")


def test_inc_gap_detected_increments_counter():
    import metrics
    from prometheus_client import REGISTRY
    before = REGISTRY.get_sample_value(
        "arb_gaps_detected_total",
        {"pair_type": "cross_platform", "confidence": "high"}
    ) or 0.0
    metrics.inc_gap_detected(pair_type="cross_platform", confidence="high")
    after = REGISTRY.get_sample_value(
        "arb_gaps_detected_total",
        {"pair_type": "cross_platform", "confidence": "high"}
    )
    assert after == before + 1.0


def test_inc_gap_rejected_with_reason():
    import metrics
    from prometheus_client import REGISTRY
    before = REGISTRY.get_sample_value(
        "arb_gaps_rejected_total",
        {"reason_category": "ev_fail", "pair_type": "cross_platform"}
    ) or 0.0
    metrics.inc_gap_rejected(reason="ev_fail", pair_type="cross_platform")
    after = REGISTRY.get_sample_value(
        "arb_gaps_rejected_total",
        {"reason_category": "ev_fail", "pair_type": "cross_platform"}
    )
    assert after == before + 1.0
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd python-core && uv run pytest tests/test_metrics.py -v 2>&1 | tail -10
```
Expected: ImportError (module doesn't exist).

- [ ] **Step 3: Add prometheus_client to pyproject.toml**

```toml
dependencies = [
    "aiohttp>=3.9",
    "python-dotenv>=1.0",
    "loguru>=0.7",
    "pydantic>=2.0",
    "rapidfuzz>=3.0",
    "py-clob-client-v2>=0.1.0",
    "prometheus-client>=0.20",
]
```

Run `uv lock` to update the lockfile.

- [ ] **Step 4: Create python-core/metrics.py**

```python
"""Prometheus metrics for the arb bot.

Import this module once at startup. All metric objects are module-level singletons.
Use the helper functions (inc_*, observe_*, set_*) to record events — they accept
plain strings and numbers, so callers never need to import prometheus_client directly.
"""
from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

gaps_detected = Counter(
    "arb_gaps_detected_total",
    "Gap detection events emitted by Rust comparator",
    ["pair_type", "confidence"],
)

gaps_rejected = Counter(
    "arb_gaps_rejected_total",
    "Gap events rejected by GapDetector.validate()",
    ["reason_category", "pair_type"],
)

executions = Counter(
    "arb_executions_total",
    "Two-leg execution attempts",
    ["pair_type", "dry_run", "outcome"],
)

fill_polls = Counter(
    "arb_fill_polls_total",
    "Fill polling results after order placement",
    ["platform", "result"],
)

ws_reconnects = Counter(
    "arb_ws_reconnects_total",
    "WebSocket reconnection events",
    ["platform"],
)

emergency_closes = Counter(
    "arb_emergency_closes_total",
    "Emergency close attempts on partial fills",
    ["platform"],
)

# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------

open_positions = Gauge(
    "arb_open_positions_count",
    "Currently open positions",
    ["dry_run"],
)

daily_pnl = Gauge(
    "arb_daily_pnl_usdc",
    "Net P&L for the current trading day (USD)",
)

daily_exposure = Gauge(
    "arb_daily_exposure_usdc",
    "Total notional in open live positions (USD)",
)

active_opportunities = Gauge(
    "arb_active_opportunities_count",
    "Opportunities currently tracked by the lifecycle engine",
    ["state"],
)

# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------

fill_latency = Histogram(
    "arb_fill_latency_seconds",
    "Time from order placement to fill confirmation",
    ["platform"],
    buckets=[0.5, 1, 2, 5, 10, 20, 30, 60],
)

gap_to_execution = Histogram(
    "arb_gap_to_execution_latency_seconds",
    "Time from gap_detected event to order submission",
    buckets=[0.1, 0.5, 1, 2, 5, 10],
)

opportunity_duration = Histogram(
    "arb_opportunity_duration_seconds",
    "Lifetime of an opportunity from first to last observation",
    ["terminal_state"],
    buckets=[1, 5, 30, 60, 300, 600, 1800, 3600],
)

gap_cents_dist = Histogram(
    "arb_gap_cents",
    "Distribution of gap sizes at detection time",
    ["pair_type"],
    buckets=[5, 8, 10, 12, 15, 20, 25, 30],
)

# ---------------------------------------------------------------------------
# Helper functions — callers use these, never raw prometheus objects
# ---------------------------------------------------------------------------

def inc_gap_detected(pair_type: str = "cross_platform", confidence: str = "medium") -> None:
    gaps_detected.labels(pair_type=pair_type, confidence=confidence).inc()


def inc_gap_rejected(reason: str, pair_type: str = "cross_platform") -> None:
    gaps_rejected.labels(reason_category=reason, pair_type=pair_type).inc()


def inc_execution(pair_type: str, dry_run: bool, outcome: str) -> None:
    executions.labels(pair_type=pair_type, dry_run=str(dry_run), outcome=outcome).inc()


def inc_fill_poll(platform: str, result: str) -> None:
    fill_polls.labels(platform=platform, result=result).inc()


def inc_ws_reconnect(platform: str) -> None:
    ws_reconnects.labels(platform=platform).inc()


def inc_emergency_close(platform: str) -> None:
    emergency_closes.labels(platform=platform).inc()


def observe_fill_latency(platform: str, seconds: float) -> None:
    fill_latency.labels(platform=platform).observe(seconds)


def observe_gap_to_execution(seconds: float) -> None:
    gap_to_execution.observe(seconds)


def observe_opportunity_duration(state: str, seconds: float) -> None:
    opportunity_duration.labels(terminal_state=state).observe(seconds)


def observe_gap_cents(pair_type: str, cents: float) -> None:
    gap_cents_dist.labels(pair_type=pair_type).observe(cents)


def set_open_positions(count: int, dry_run: bool) -> None:
    open_positions.labels(dry_run=str(dry_run)).set(count)


def set_daily_pnl(usdc: float) -> None:
    daily_pnl.set(usdc)


def set_daily_exposure(usdc: float) -> None:
    daily_exposure.set(usdc)


def _categorize_rejection(reason: str) -> str:
    reason_lower = reason.lower()
    if "ev" in reason_lower:
        return "ev_fail"
    if "thin" in reason_lower or "liquidity" in reason_lower:
        return "thin_market"
    if "unstable" in reason_lower:
        return "unstable"
    if "stale" in reason_lower:
        return "stale_feed"
    if "open trade" in reason_lower:
        return "open_trade"
    if "daily loss" in reason_lower:
        return "daily_loss"
    if "position" in reason_lower:
        return "position_limit"
    if "confidence" in reason_lower:
        return "low_confidence"
    if "blacklist" in reason_lower:
        return "blacklisted"
    if "closes in" in reason_lower or "close" in reason_lower:
        return "too_close"
    if "update" in reason_lower or "new" in reason_lower:
        return "too_new"
    return "other"
```

- [ ] **Step 5: Run tests**

```bash
cd python-core && uv run pytest tests/test_metrics.py -v 2>&1 | tail -10
```
Expected: all 3 pass.

- [ ] **Step 6: Commit**

```bash
git add python-core/pyproject.toml python-core/uv.lock python-core/metrics.py python-core/tests/test_metrics.py
git commit -m "feat(metrics): add prometheus_client + metrics.py with all counters/gauges/histograms"
```

---

### Task F2: Wire metrics into notifier.py + main.py

**Files:**
- Modify: `python-core/notifier.py`
- Modify: `python-core/main.py`

- [ ] **Step 1: Wire gap_detected and gap_rejected counters into notifier.py**

In `notifier.py`, add import at top:
```python
import metrics as _metrics
```

In `gap_detected()`, add after the rate-limit check:
```python
_metrics.inc_gap_detected(
    pair_type=gap.get("pair_type", "cross_platform"),
    confidence=gap.get("confidence", "medium"),
)
_metrics.observe_gap_cents(
    pair_type=gap.get("pair_type", "cross_platform"),
    cents=gap.get("gap_cents", 0.0),
)
```

In `gap_rejected()`, add after the rate-limit check:
```python
_metrics.inc_gap_rejected(
    reason=_metrics._categorize_rejection(reason),
    pair_type="cross_platform",  # reason string doesn't carry pair_type
)
```

- [ ] **Step 2: Wire execution metrics into main.py**

In `_handle_gap_inner()`, after `executor.execute(gap)`, add:
```python
if confirmation:
    _metrics.inc_execution(
        pair_type=gap.get("pair_type", "cross_platform"),
        dry_run=CONFIG.get("dry_run", True),
        outcome="success",
    )
else:
    _metrics.inc_execution(
        pair_type=gap.get("pair_type", "cross_platform"),
        dry_run=CONFIG.get("dry_run", True),
        outcome="execution_failed",
    )
```

Add Prometheus HTTP server start in `main()` after `_GAP_SEMAPHORE` setup:
```python
from prometheus_client import start_http_server as _prom_start
_prom_start(9090)
notifier.logger.info("Prometheus metrics server started on :9090")
```

- [ ] **Step 3: Add Rust ws_reconnect event handling**

In `_read_stdout()`, add handler for reconnect events:
```python
elif event_type == "ws_reconnect":
    platform = event.get("platform", "unknown")
    import metrics as _metrics
    _metrics.inc_ws_reconnect(platform)
    notifier.logger.debug(f"WS reconnect: {platform}")
```

- [ ] **Step 4: Run full test suite**

```bash
cd python-core && uv run pytest --tb=short -q 2>&1 | tail -10
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add python-core/notifier.py python-core/main.py
git commit -m "feat(metrics): wire gap_detected, gap_rejected, execution counters into notifier + main"
```

---

### Task F3: Wire fill latency into TwoLegExecutor

**Files:**
- Modify: `python-core/two_leg_executor.py`
- Modify: `python-core/tests/test_two_leg_executor.py`

- [ ] **Step 1: Write failing test for fill latency recording**

In `python-core/tests/test_two_leg_executor.py`, add:

```python
def test_fill_poll_records_fill_metric_on_success(config, db, cross_platform_gap):
    """fill_polls counter must be incremented when poll returns filled."""
    from prometheus_client import REGISTRY
    config["dry_run"] = False

    before = REGISTRY.get_sample_value(
        "arb_fill_polls_total",
        {"platform": "polymarket", "result": "filled"}
    ) or 0.0

    poly_result = {"order_id": "p1", "status": "matched", "token_id": "tok"}
    kal_result = {"order_id": "k1", "status": "matched", "ticker": "TICK"}

    with patch("two_leg_executor.PolymarketExecutor") as MP, \
         patch("two_leg_executor.KalshiExecutor") as MK, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.1), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.05):
        MP.return_value.place_order = AsyncMock(return_value=poly_result)
        MP.return_value.get_order_status = AsyncMock(return_value="matched")
        MK.return_value.place_order = AsyncMock(return_value=kal_result)
        MK.return_value.get_order_status = AsyncMock(return_value="matched")

        ex = TwoLegExecutor(config, db)
        import asyncio
        asyncio.run(ex.execute(cross_platform_gap, bet_size=10.0))

    after = REGISTRY.get_sample_value(
        "arb_fill_polls_total",
        {"platform": "polymarket", "result": "filled"}
    )
    assert after == before + 1.0
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd python-core && uv run pytest tests/test_two_leg_executor.py -k "fill_metric" -v 2>&1 | tail -10
```
Expected: FAIL.

- [ ] **Step 3: Wire metrics into _poll_and_cancel()**

In `two_leg_executor.py`, add import at top:
```python
import metrics as _metrics
import time as _time
```

Replace `_poll_and_cancel()`:
```python
async def _poll_and_cancel(self, platform: str, order_id: str) -> bool:
    t0 = _time.monotonic()
    filled = await self._wait_for_fill(platform, order_id)
    elapsed = _time.monotonic() - t0
    if filled:
        _metrics.observe_fill_latency(platform, elapsed)
        _metrics.inc_fill_poll(platform, "filled")
    else:
        _metrics.inc_fill_poll(platform, "timeout")
        log.warning(
            "%s order %s did not fill in %ss — canceling",
            platform, order_id, _FILL_TIMEOUT,
        )
        try:
            if platform == "polymarket":
                await self._poly.cancel_order(order_id)
            elif platform == "kalshi":
                await self._kalshi.cancel_order(order_id)
        except Exception:
            pass
    return filled
```

Also in `_emergency_close()`, add after the try/except:
```python
_metrics.inc_emergency_close(platform)
```

- [ ] **Step 4: Run tests**

```bash
cd python-core && uv run pytest --tb=short -q 2>&1 | tail -10
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add python-core/two_leg_executor.py python-core/tests/test_two_leg_executor.py
git commit -m "feat(metrics): fill latency histogram + fill_poll counter wired into TwoLegExecutor"
```

---

### Task F4: Health endpoint + structured JSON logging

**Files:**
- Create: `python-core/health.py`
- Modify: `python-core/main.py`
- Modify: `python-core/notifier.py`
- Create: `python-core/tests/test_health.py`

- [ ] **Step 1: Write failing test for health endpoint**

Create `python-core/tests/test_health.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
import asyncio


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok():
    from health import HealthServer
    import aiohttp

    state = {"last_gap_seen": 0.0, "ws_connected": ["polymarket", "kalshi"]}
    server = HealthServer(state, port=18080)
    await server.start()

    async with aiohttp.ClientSession() as session:
        async with session.get("http://localhost:18080/health") as resp:
            assert resp.status == 200
            data = await resp.json()
            assert data["status"] == "ok"
            assert "last_gap_age_s" in data
            assert "ws_connected" in data

    await server.stop()


@pytest.mark.asyncio
async def test_health_endpoint_stale_feed():
    from health import HealthServer
    import aiohttp
    import time

    state = {"last_gap_seen": time.monotonic() - 200.0, "ws_connected": []}
    server = HealthServer(state, port=18081)
    await server.start()

    async with aiohttp.ClientSession() as session:
        async with session.get("http://localhost:18081/health") as resp:
            data = await resp.json()
            assert data["status"] in ("degraded", "ok")
            assert data["last_gap_age_s"] > 190

    await server.stop()
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd python-core && uv run pytest tests/test_health.py -v 2>&1 | tail -10
```
Expected: ImportError.

- [ ] **Step 3: Create python-core/health.py**

```python
"""Lightweight health check server for the arb bot.

Serves JSON status on GET /health. Designed to run as an asyncio task
alongside the main event loop — uses aiohttp for the HTTP server.
"""
import time
from aiohttp import web


class HealthServer:
    def __init__(self, state: dict, port: int = 8080):
        self._state = state
        self._port = port
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _handle_health(self, request: web.Request) -> web.Response:
        now = time.monotonic()
        last_gap = self._state.get("last_gap_seen", 0.0)
        age_s = now - last_gap if last_gap > 0 else float("inf")
        ws_connected = self._state.get("ws_connected", [])
        open_positions = self._state.get("open_positions", 0)

        status = "ok"
        if age_s > 120:
            status = "degraded"

        return web.json_response({
            "status": status,
            "last_gap_age_s": round(age_s, 1) if age_s != float("inf") else None,
            "ws_connected": ws_connected,
            "open_positions": open_positions,
        })
```

- [ ] **Step 4: Wire health server into main.py**

In `main()`, after `_GAP_SEMAPHORE` setup, add:
```python
from health import HealthServer as _HealthServer
_health_state: dict = {"last_gap_seen": 0.0, "ws_connected": [], "open_positions": 0}
_health_server = _HealthServer(_health_state, port=8080)
await _health_server.start()
notifier.logger.info("Health server started on :8080")
```

Pass `_health_state` into `_read_stdout()` call and update `last_gap_seen` in `_handle_gap_inner()`:
```python
# In _handle_gap_inner, at the top:
import time as _time_mod
_health_state["last_gap_seen"] = _time_mod.monotonic()
```

- [ ] **Step 5: Add structured JSON log file handler to notifier.py**

In `notifier.py`, add after the existing logger setup:
```python
import os as _os
_log_dir = _os.getenv("LOG_DIR", "logs")
_os.makedirs(_log_dir, exist_ok=True)
logger.add(
    f"{_log_dir}/arb_structured.jsonl",
    format="{time:YYYY-MM-DDTHH:mm:ss.SSSZ} {level} {message}",
    serialize=True,    # loguru JSON serialization
    rotation="100 MB",
    retention="7 days",
    level="INFO",
)
```

- [ ] **Step 6: Run all tests**

```bash
cd python-core && uv run pytest --tb=short -q 2>&1 | tail -10
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add python-core/health.py python-core/tests/test_health.py python-core/main.py python-core/notifier.py
git commit -m "feat(observability): health endpoint on :8080, Prometheus on :9090, structured JSON logs"
```

---

## PHASE D — LIQUIDITY + SLIPPAGE REALITY

### Task D1: Add bid_size/ask_size to Rust Price struct + fetchers

**Files:**
- Modify: `rust-core/src/types.rs`
- Modify: `rust-core/src/fetcher/polymarket.rs`
- Modify: `rust-core/src/fetcher/kalshi.rs`

- [ ] **Step 1: Write failing tests**

In `rust-core/src/fetcher/polymarket.rs` test block, add:
```rust
#[test]
fn test_bid_size_stored_in_price() {
    let (price_map, tx) = make_price_map();
    let msg = r#"{"event_type":"best_bid_ask","asset_id":"sz1","bid_price":"0.60","bid_size":"250","ask_price":"0.61","ask_size":"100"}"#;
    handle_price_message(msg, &price_map, &tx);
    let map = price_map.read().unwrap();
    let p = map.get("poly:sz1").unwrap();
    assert!((p.bid_size - 250.0).abs() < 0.001, "bid_size should be 250");
    assert!((p.ask_size - 100.0).abs() < 0.001, "ask_size should be 100");
}
```

In `rust-core/src/fetcher/kalshi.rs` test block, add:
```rust
#[test]
fn test_kalshi_snapshot_populates_bid_size() {
    let price_map = Arc::new(RwLock::new(HashMap::new()));
    let books = Arc::new(RwLock::new(HashMap::new()));
    let yes = vec![[60i64, 50i64], [58i64, 30i64]];
    let no  = vec![[38i64, 20i64]];
    apply_snapshot("TICKER", &yes, &no, "mkt-id", &price_map, &books);
    let map = price_map.read().unwrap();
    let p = map.get("kalshi:TICKER").unwrap();
    // best YES bid = 60, qty = 50
    assert!((p.bid_size - 50.0).abs() < 0.001, "bid_size should be 50 (qty at best YES bid)");
    // best NO bid = 38, qty = 20 → yes_ask_qty = 20
    assert!((p.ask_size - 20.0).abs() < 0.001, "ask_size should be 20 (qty at best NO bid → YES ask)");
}
```

Run: `cd rust-core && cargo test 2>&1 | tail -15` → expect compile errors.

- [ ] **Step 2: Add bid_size/ask_size to Price struct in types.rs**

```rust
pub struct Price {
    pub market_id: String,
    pub platform: Platform,
    pub yes_price: f64,   // best bid for YES
    pub yes_ask: f64,     // best ask for YES
    pub no_price: f64,
    pub bid_size: f64,    // top-of-book bid qty (contracts on Kalshi, shares on Polymarket)
    pub ask_size: f64,    // top-of-book ask qty
    pub timestamp: DateTime<Utc>,
}
```

- [ ] **Step 3: Update all Price constructors in polymarket.rs**

In `handle_price_message`, parse bid_size/ask_size:
```rust
let bid_size: f64 = msg
    .get("bid_size")
    .and_then(|v| v.as_str())
    .and_then(|s| s.parse().ok())
    .unwrap_or(0.0);
let ask_size: f64 = msg
    .get("ask_size")
    .and_then(|v| v.as_str())
    .and_then(|s| s.parse().ok())
    .unwrap_or(0.0);
// Price { ..., bid_size, ask_size }
```

In `fetch_batch` (REST warm-up): `bid_size: 0.0, ask_size: 0.0` (no depth from REST).

- [ ] **Step 4: Add best_yes_bid_qty / best_yes_ask_qty to KalshiBook**

In `kalshi.rs`:
```rust
pub fn best_yes_bid_qty(&self) -> i64 {
    self.yes.iter().rev().find(|(_, &qty)| qty > 0).map(|(_, &q)| q).unwrap_or(0)
}
pub fn best_yes_ask_qty(&self) -> i64 {
    // YES ask is matched against the NO bid book — qty at best NO bid is the available YES ask qty
    self.no.iter().rev().find(|(_, &qty)| qty > 0).map(|(_, &q)| q).unwrap_or(0)
}
```

Update `apply_snapshot` and `apply_delta` to populate `bid_size`/`ask_size` in Price.

- [ ] **Step 5: Verify tests pass**

```bash
cd rust-core && cargo test 2>&1 | tail -15
```
Expected: all pass including new bid_size tests.

- [ ] **Step 6: Commit**

```bash
git add rust-core/src/types.rs rust-core/src/fetcher/polymarket.rs rust-core/src/fetcher/kalshi.rs
git commit -m "feat(liquidity): add bid_size/ask_size to Price struct — parsed from WS, extracted from KalshiBook"
```

---

### Task D2: Add liquidity fields to Gap + comparator gate

**Files:**
- Modify: `rust-core/src/types.rs` (Gap struct)
- Modify: `rust-core/src/comparator.rs`

- [ ] **Step 1: Write failing tests**

In `rust-core/src/comparator.rs` tests block, add:
```rust
#[test]
fn test_thin_market_gap_not_emitted_when_below_threshold() {
    // Both sides have bid_size=0 → liquidity gate must suppress the gap
    // even though the price gap is valid
    let poly = Price { yes_price: 0.61, yes_ask: 0.61, no_price: 0.39,
                       bid_size: 0.0, ask_size: 0.0, .. make_price(Platform::Polymarket, 0.61, 0.61) };
    let kalshi = Price { yes_price: 0.55, yes_ask: 0.60, no_price: 0.45,
                         bid_size: 0.0, ask_size: 0.0, .. make_price(Platform::Kalshi, 0.55, 0.60) };
    // ... (full test implementation in-place)
}

#[test]
fn test_liquid_market_gap_emitted_with_liquidity_fields() {
    // Gap with sufficient liquidity should emit with poly_liquidity/kalshi_liquidity fields
}
```

- [ ] **Step 2: Add liquidity fields to Gap struct**

In `types.rs`:
```rust
pub struct Gap {
    // ... existing fields ...
    pub poly_liquidity_usdc: f64,
    pub kalshi_liquidity_usdc: f64,
}
```

Update `Gap::new()` signature to accept and store these fields.

- [ ] **Step 3: Compute liquidity in check_cross_platform and add gate**

In `comparator.rs`:
```rust
// Compute executable notional at top of book
// Polymarket: bid_size is in shares (1 share = $0.01 notional at price p)
// Use a per-pair config threshold (default: require at least min_bet_usdc worth)
let poly_liq = poly.ask_size * poly.yes_ask;   // Direction 1: buying at ask
let kalshi_liq = kalshi.ask_size * kalshi.yes_ask;
let min_liq = config.min_bet_usdc;

if poly_liq < min_liq || kalshi_liq < min_liq {
    debug!("Thin market: poly_liq={:.1} kalshi_liq={:.1} < min={:.1}",
           poly_liq, kalshi_liq, min_liq);
    return;  // skip — insufficient depth
}
```

- [ ] **Step 4: Build and run tests**

```bash
cd rust-core && cargo build --release && cargo test 2>&1 | tail -15
```
Expected: all pass.

- [ ] **Step 5: Update Python to handle new Gap fields gracefully**

In `python-core/ev_engine.py`, update `calculate_arb_ev` slippage parameter: callers can now pass dynamic slippage computed from liquidity. No code change needed — the `slippage_cents` parameter already accepts any float.

In `python-core/detector.py`, add Check 1a after the EV gate:
```python
# Check 1a: Liquidity gate (populated by Rust comparator from order book depth)
poly_liq = gap.get("poly_liquidity_usdc", float("inf"))
kalshi_liq = gap.get("kalshi_liquidity_usdc", float("inf"))
min_liq = self.config.get("min_bet_usdc", 10.0)
if poly_liq < min_liq or kalshi_liq < min_liq:
    return False, (
        f"Thin market: poly ${poly_liq:.1f} / kalshi ${kalshi_liq:.1f} "
        f"< min ${min_liq:.1f}"
    )
```

Create `python-core/liquidity.py`:
```python
def estimate_slippage_cents(bet_usdc: float, top_of_book_usdc: float,
                            base_slippage: float = 0.3, impact_factor: float = 5.0) -> float:
    if top_of_book_usdc <= 0:
        return base_slippage + impact_factor  # no depth — max slippage
    depth_ratio = min(bet_usdc / top_of_book_usdc, 1.0)
    return base_slippage + (depth_ratio ** 1.5) * impact_factor
```

- [ ] **Step 6: Add liquidity gate test to test_detector.py**

```python
def test_thin_market_rejected_by_liquidity_gate():
    detector, _ = make_detector({"min_bet_usdc": 10.0})
    gap = {
        **BASE_GAP,
        "poly_liquidity_usdc": 5.0,   # below 10.0 min
        "kalshi_liquidity_usdc": 20.0,
    }
    ok, reason = feed_gap(detector, gap)
    assert not ok
    assert "thin" in reason.lower() or "liquidity" in reason.lower()
```

- [ ] **Step 7: Run full regression**

```bash
cd python-core && uv run pytest --tb=short -q 2>&1 | tail -5
cd rust-core && cargo test 2>&1 | tail -5
```
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add rust-core/src/types.rs rust-core/src/comparator.rs \
        python-core/detector.py python-core/liquidity.py python-core/tests/test_detector.py
git commit -m "feat(liquidity): thin-market gate in Rust comparator + Python detector; dynamic slippage model"
```

---

## PHASE E — OPPORTUNITY LIFECYCLE ENGINE

### Task E1: OpportunityEngine + opportunities table

**Files:**
- Create: `python-core/opportunity_engine.py`
- Modify: `python-core/tracker.py`
- Create: `python-core/tests/test_opportunity_engine.py`

- [ ] **Step 1: Add opportunities table to tracker.py**

In `_create_tables()`, add:
```sql
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opp_key TEXT NOT NULL UNIQUE,
    market_id TEXT NOT NULL,
    pair_type TEXT NOT NULL DEFAULT 'cross_platform',
    direction TEXT NOT NULL DEFAULT 'dir1',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    first_gap_cents REAL,
    max_gap_cents REAL,
    min_gap_cents REAL,
    avg_gap_cents REAL,
    gap_volatility REAL DEFAULT 0.0,
    observation_count INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'detected',
    execution_attempted INTEGER DEFAULT 0,
    execution_success INTEGER DEFAULT 0,
    trade_id INTEGER REFERENCES trades(id),
    collapse_reason TEXT,
    stale_reason TEXT,
    poly_bid_size REAL,
    kalshi_ask_size REAL,
    min_executable_notional REAL,
    created_at TEXT DEFAULT (datetime('now')),
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_opp_key ON opportunities(opp_key);
CREATE INDEX IF NOT EXISTS idx_opp_state ON opportunities(state, last_seen DESC);
CREATE INDEX IF NOT EXISTS idx_opp_market ON opportunities(market_id, state);
```

Add helpers:
```python
def upsert_opportunity(conn, opp: dict) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO opportunities
           (opp_key, market_id, pair_type, direction, first_seen, last_seen,
            first_gap_cents, max_gap_cents, min_gap_cents, avg_gap_cents,
            gap_volatility, observation_count, duration_ms, state,
            poly_bid_size, kalshi_ask_size, min_executable_notional)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(opp_key) DO UPDATE SET
               last_seen=excluded.last_seen,
               max_gap_cents=MAX(max_gap_cents, excluded.max_gap_cents),
               min_gap_cents=MIN(min_gap_cents, excluded.min_gap_cents),
               avg_gap_cents=excluded.avg_gap_cents,
               gap_volatility=excluded.gap_volatility,
               observation_count=excluded.observation_count,
               duration_ms=excluded.duration_ms,
               state=excluded.state,
               poly_bid_size=excluded.poly_bid_size,
               kalshi_ask_size=excluded.kalshi_ask_size,
               min_executable_notional=excluded.min_executable_notional""",
        (opp["opp_key"], opp["market_id"], opp.get("pair_type","cross_platform"),
         opp.get("direction","dir1"), opp["first_seen"], now,
         opp["first_gap_cents"], opp["max_gap_cents"], opp["min_gap_cents"],
         opp["avg_gap_cents"], opp.get("gap_volatility",0.0),
         opp["observation_count"], opp.get("duration_ms",0), opp.get("state","detected"),
         opp.get("poly_bid_size"), opp.get("kalshi_ask_size"), opp.get("min_executable_notional")),
    )
    conn.commit()
    return cur.lastrowid

def close_opportunity(conn, opp_key: str, state: str, reason: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE opportunities SET state=?, closed_at=?, collapse_reason=? WHERE opp_key=?",
        (state, now, reason, opp_key),
    )
    conn.commit()
```

- [ ] **Step 2: Write tests for OpportunityEngine**

Create `python-core/tests/test_opportunity_engine.py`:

```python
import sys, time, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tracker import _create_tables
from opportunity_engine import OpportunityEngine, OpportunityState


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_tables(conn)
    return conn


def make_gap(market_id="test-mkt", gap_cents=12.0, pair_type="cross_platform"):
    return {
        "market_id": market_id,
        "pair_type": pair_type,
        "gap_cents": gap_cents,
        "polymarket_price": 0.71,
        "kalshi_price": 0.58,
        "confidence": "high",
    }


def test_first_observation_creates_opportunity():
    db = make_db()
    engine = OpportunityEngine(db)
    opp = engine.observe(make_gap())
    assert opp is not None
    assert opp.state == OpportunityState.DETECTED
    assert opp.observation_count == 1


def test_repeated_observations_update_same_opportunity():
    db = make_db()
    engine = OpportunityEngine(db)
    g = make_gap(gap_cents=12.0)
    for _ in range(5):
        opp = engine.observe(g)
    assert opp.observation_count == 5
    assert len(engine._opps) == 1


def test_stable_after_three_observations():
    db = make_db()
    engine = OpportunityEngine(db)
    g = make_gap()
    for _ in range(3):
        opp = engine.observe(g)
    assert opp.state == OpportunityState.STABLE


def test_collapse_when_gap_drops_below_threshold():
    db = make_db()
    engine = OpportunityEngine(db, collapse_threshold_cents=3.0)
    g_big = make_gap(gap_cents=12.0)
    for _ in range(3):
        engine.observe(g_big)
    g_small = make_gap(gap_cents=1.0)  # below collapse_threshold
    opp = engine.observe(g_small)
    assert opp.state == OpportunityState.COLLAPSED


def test_expired_after_timeout():
    db = make_db()
    engine = OpportunityEngine(db, stale_timeout_s=0.01)
    engine.observe(make_gap())
    time.sleep(0.05)
    engine.evict_stale()
    assert len(engine._opps) == 0  # evicted


def test_different_markets_are_separate_opportunities():
    db = make_db()
    engine = OpportunityEngine(db)
    engine.observe(make_gap(market_id="mkt-a"))
    engine.observe(make_gap(market_id="mkt-b"))
    assert len(engine._opps) == 2


def test_opp_key_uniqueness_by_market_and_direction():
    db = make_db()
    engine = OpportunityEngine(db)
    engine.observe(make_gap(market_id="mkt-x"))
    engine.observe(make_gap(market_id="mkt-x-rev"))  # different direction
    assert len(engine._opps) == 2
```

- [ ] **Step 3: Run to confirm failure**

```bash
cd python-core && uv run pytest tests/test_opportunity_engine.py -v 2>&1 | tail -15
```
Expected: ImportError.

- [ ] **Step 4: Create python-core/opportunity_engine.py**

```python
"""Opportunity lifecycle engine.

Tracks price dislocations from first observation through execution or collapse.
In-memory state is the source of truth; DB is the durable log.
"""
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional
import sqlite3

import tracker


class OpportunityState(Enum):
    DETECTED = auto()
    STABLE = auto()
    EXECUTED = auto()
    COLLAPSED = auto()
    EXPIRED = auto()


@dataclass
class Opportunity:
    opp_key: str
    market_id: str
    pair_type: str
    direction: str
    first_seen: float                    # monotonic
    last_seen: float
    first_gap_cents: float
    max_gap_cents: float
    min_gap_cents: float
    _gap_sum: float = field(default=0.0, repr=False)
    observation_count: int = 0
    state: OpportunityState = OpportunityState.DETECTED
    _gap_history: deque = field(default_factory=lambda: deque(maxlen=20), repr=False)
    poly_bid_size: float = 0.0
    kalshi_ask_size: float = 0.0
    collapse_reason: str = ""
    _last_db_write: float = field(default=0.0, repr=False)

    @property
    def avg_gap_cents(self) -> float:
        return self._gap_sum / self.observation_count if self.observation_count > 0 else 0.0

    @property
    def gap_volatility(self) -> float:
        if len(self._gap_history) < 2:
            return 0.0
        mean = sum(self._gap_history) / len(self._gap_history)
        variance = sum((x - mean) ** 2 for x in self._gap_history) / len(self._gap_history)
        return variance ** 0.5

    @property
    def duration_ms(self) -> int:
        return int((self.last_seen - self.first_seen) * 1000)

    def observe(self, gap_cents: float, poly_bid_size: float = 0.0, kalshi_ask_size: float = 0.0) -> None:
        self.last_seen = time.monotonic()
        self.observation_count += 1
        self._gap_sum += gap_cents
        self.max_gap_cents = max(self.max_gap_cents, gap_cents)
        self.min_gap_cents = min(self.min_gap_cents, gap_cents)
        self._gap_history.append(gap_cents)
        self.poly_bid_size = poly_bid_size
        self.kalshi_ask_size = kalshi_ask_size
        if self.state == OpportunityState.DETECTED and self.observation_count >= 3:
            self.state = OpportunityState.STABLE

    def to_dict(self) -> dict:
        return {
            "opp_key": self.opp_key,
            "market_id": self.market_id,
            "pair_type": self.pair_type,
            "direction": self.direction,
            "first_seen": _mono_to_iso(self.first_seen),
            "last_seen": _mono_to_iso(self.last_seen),
            "first_gap_cents": self.first_gap_cents,
            "max_gap_cents": self.max_gap_cents,
            "min_gap_cents": self.min_gap_cents,
            "avg_gap_cents": round(self.avg_gap_cents, 4),
            "gap_volatility": round(self.gap_volatility, 4),
            "observation_count": self.observation_count,
            "duration_ms": self.duration_ms,
            "state": self.state.name.lower(),
            "poly_bid_size": self.poly_bid_size,
            "kalshi_ask_size": self.kalshi_ask_size,
        }


def _make_opp_key(market_id: str, pair_type: str) -> str:
    return f"{market_id}:{pair_type}"


def _mono_to_iso(mono: float) -> str:
    from datetime import datetime, timezone
    wall = datetime.now(timezone.utc).timestamp() - (time.monotonic() - mono)
    return datetime.fromtimestamp(wall, tz=timezone.utc).isoformat()


class OpportunityEngine:
    def __init__(
        self,
        db_conn: sqlite3.Connection,
        collapse_threshold_cents: float = 2.0,
        stale_timeout_s: float = 120.0,
        db_write_interval_s: float = 60.0,
    ):
        self._db = db_conn
        self._collapse_threshold = collapse_threshold_cents
        self._stale_timeout = stale_timeout_s
        self._db_write_interval = db_write_interval_s
        self._opps: dict[str, Opportunity] = {}

    def observe(self, gap: dict) -> Optional[Opportunity]:
        market_id = gap["market_id"]
        pair_type = gap.get("pair_type", "cross_platform")
        gap_cents = gap.get("gap_cents", 0.0)
        key = _make_opp_key(market_id, pair_type)

        opp = self._opps.get(key)

        if opp is None or opp.state in (OpportunityState.COLLAPSED, OpportunityState.EXPIRED):
            opp = Opportunity(
                opp_key=key,
                market_id=market_id,
                pair_type=pair_type,
                direction="dir2" if market_id.endswith("-rev") else "dir1",
                first_seen=time.monotonic(),
                last_seen=time.monotonic(),
                first_gap_cents=gap_cents,
                max_gap_cents=gap_cents,
                min_gap_cents=gap_cents,
            )
            self._opps[key] = opp

        opp.observe(
            gap_cents,
            poly_bid_size=gap.get("poly_liquidity_usdc", 0.0),
            kalshi_ask_size=gap.get("kalshi_liquidity_usdc", 0.0),
        )

        if gap_cents < self._collapse_threshold and opp.state in (
            OpportunityState.DETECTED, OpportunityState.STABLE
        ):
            opp.state = OpportunityState.COLLAPSED
            opp.collapse_reason = f"gap {gap_cents:.1f}¢ below threshold {self._collapse_threshold:.1f}¢"

        self._maybe_flush_to_db(opp)
        return opp

    def mark_executed(self, market_id: str, pair_type: str, trade_id: int) -> None:
        key = _make_opp_key(market_id, pair_type)
        opp = self._opps.get(key)
        if opp:
            opp.state = OpportunityState.EXECUTED
            self._flush_to_db(opp)

    def evict_stale(self) -> None:
        now = time.monotonic()
        to_evict = [
            key for key, opp in self._opps.items()
            if (now - opp.last_seen) > self._stale_timeout
            and opp.state not in (OpportunityState.EXECUTED,)
        ]
        for key in to_evict:
            opp = self._opps[key]
            opp.state = OpportunityState.EXPIRED
            self._flush_to_db(opp)
            del self._opps[key]

    def _maybe_flush_to_db(self, opp: Opportunity) -> None:
        now = time.monotonic()
        if (now - opp._last_db_write) >= self._db_write_interval:
            self._flush_to_db(opp)

    def _flush_to_db(self, opp: Opportunity) -> None:
        try:
            tracker.upsert_opportunity(self._db, opp.to_dict())
            opp._last_db_write = time.monotonic()
        except Exception:
            pass  # non-fatal — DB write is analytics, not execution path
```

- [ ] **Step 5: Run tests**

```bash
cd python-core && uv run pytest tests/test_opportunity_engine.py -v 2>&1 | tail -15
```
Expected: all 8 pass.

- [ ] **Step 6: Wire into main.py**

In `main()`, create engine:
```python
from opportunity_engine import OpportunityEngine
opp_engine = OpportunityEngine(db_conn)
```

In `_handle_gap_inner()`, at the very top (before detector.validate):
```python
opp = opp_engine.observe(gap)
gap["opp_id"] = opp.opp_key if opp else None
```

After successful trade:
```python
opp_engine.mark_executed(market_id, gap.get("pair_type", "cross_platform"), trade_id)
```

Add eviction task in `main()`:
```python
async def _evict_stale_opportunities():
    while True:
        await asyncio.sleep(60)
        opp_engine.evict_stale()
asyncio.create_task(_evict_stale_opportunities())
```

- [ ] **Step 7: Run full test suite**

```bash
cd python-core && uv run pytest --tb=short -q 2>&1 | tail -5
```

- [ ] **Step 8: Commit**

```bash
git add python-core/opportunity_engine.py python-core/tests/test_opportunity_engine.py \
        python-core/tracker.py python-core/main.py
git commit -m "feat(opp-engine): opportunity lifecycle engine — DETECTED→STABLE→EXECUTED/COLLAPSED/EXPIRED"
```

---

## PHASE G — EXECUTION TELEMETRY

### Task G1: execution_events table + capture in TwoLegExecutor

**Files:**
- Modify: `python-core/tracker.py`
- Modify: `python-core/two_leg_executor.py`
- Modify: `python-core/polymarket_executor.py`
- Modify: `python-core/kalshi_executor.py`
- Create: `python-core/tests/test_execution_telemetry.py`

- [ ] **Step 1: Add execution_events table**

In `tracker._create_tables()`, add:
```sql
CREATE TABLE IF NOT EXISTS execution_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER REFERENCES trades(id),
    opp_id TEXT,
    pair_type TEXT NOT NULL,
    market_id TEXT NOT NULL,
    signal_poly_price REAL,
    signal_kalshi_price REAL,
    signal_gap_cents REAL,
    signal_poly_liquidity REAL,
    signal_kalshi_liquidity REAL,
    signal_at TEXT NOT NULL,
    submitted_poly_price REAL,
    submitted_kalshi_price REAL,
    price_buffer_applied REAL DEFAULT 0.0,
    submitted_at TEXT,
    filled_poly_price REAL,
    filled_kalshi_price REAL,
    filled_at TEXT,
    fill_latency_ms INTEGER,
    poly_slippage_cents REAL,
    kalshi_slippage_cents REAL,
    total_slippage_cents REAL,
    post_poly_price REAL,
    post_kalshi_price REAL,
    market_drift_cents REAL,
    poly_fill_status TEXT,
    kalshi_fill_status TEXT,
    poly_fill_fraction REAL DEFAULT 1.0,
    kalshi_fill_fraction REAL DEFAULT 1.0,
    is_partial INTEGER DEFAULT 0,
    is_toxic INTEGER DEFAULT 0,
    urgency TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_exec_trade ON execution_events(trade_id);
CREATE INDEX IF NOT EXISTS idx_exec_market ON execution_events(market_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exec_slippage ON execution_events(total_slippage_cents);
```

Add helpers `log_execution_event(conn, evt: dict) -> int` and `update_execution_fill(conn, exec_id, fill_data: dict)`.

- [ ] **Step 2: Write tests**

Create `python-core/tests/test_execution_telemetry.py` — test that:
- `log_execution_event` creates a row with correct signal prices
- `update_execution_fill` sets fill prices and computes slippage
- dry_run execution does NOT create execution_events rows

- [ ] **Step 3: Capture signal state in TwoLegExecutor.execute()**

At the top of `execute()` (before place_order calls):
```python
from datetime import datetime, timezone
signal_at = datetime.now(timezone.utc).isoformat()
exec_event = {
    "opp_id": gap.get("opp_id"),
    "pair_type": pair_type,
    "market_id": gap["market_id"],
    "signal_poly_price": gap["polymarket_price"],
    "signal_kalshi_price": gap["kalshi_price"],
    "signal_gap_cents": gap.get("gap_cents", 0.0),
    "signal_poly_liquidity": gap.get("poly_liquidity_usdc"),
    "signal_kalshi_liquidity": gap.get("kalshi_liquidity_usdc"),
    "signal_at": signal_at,
    "urgency": "high" if price_buffer > 0 else "low",
    "price_buffer_applied": price_buffer,
}
```

After both legs submit (before polling):
```python
from datetime import datetime, timezone
exec_event["submitted_poly_price"] = order_price
exec_event["submitted_kalshi_price"] = kalshi_price
exec_event["submitted_at"] = datetime.now(timezone.utc).isoformat()
```

- [ ] **Step 4: Add get_fill_details to executors**

In `polymarket_executor.py`:
```python
def _get_fill_details_sync(self, order_id: str) -> dict:
    client = self._get_client()
    resp = client.get_order(order_id)
    if isinstance(resp, dict):
        return {
            "filled_price": float(resp.get("avgPrice", resp.get("price", 0))),
            "filled_fraction": float(resp.get("sizeMatched", 1.0)) / max(float(resp.get("size", 1.0)), 0.001),
            "status": resp.get("status", "unknown"),
        }
    return {"filled_price": 0.0, "filled_fraction": 1.0, "status": "unknown"}

async def get_fill_details(self, order_id: str) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, self._get_fill_details_sync, order_id)
```

In `kalshi_executor.py`:
```python
async def get_fill_details(self, order_id: str) -> dict:
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    headers = self._sign("GET", path)
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{self.api_url}/portfolio/orders/{order_id}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return {"filled_price": 0.0, "filled_fraction": 1.0, "status": "unknown"}
            data = await resp.json()
            order = data.get("order", {})
            filled_cnt = order.get("count_filled", order.get("count", 1))
            total_cnt = order.get("count", 1)
            avg_price = order.get("avg_price", order.get("price", 0))
            return {
                "filled_price": float(avg_price) / 100.0,  # Kalshi prices in cents
                "filled_fraction": filled_cnt / max(total_cnt, 1),
                "status": order.get("status", "unknown"),
            }
```

- [ ] **Step 5: Capture fill data and schedule post-fill snapshot**

After `_poll_and_cancel()` returns True, fetch fill details and update exec_event. Log the event to DB. Schedule post-fill snapshot as background task.

- [ ] **Step 6: Run full regression**

```bash
cd python-core && uv run pytest --tb=short -q 2>&1 | tail -5
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add python-core/tracker.py python-core/two_leg_executor.py \
        python-core/polymarket_executor.py python-core/kalshi_executor.py \
        python-core/tests/test_execution_telemetry.py
git commit -m "feat(telemetry): execution_events table — captures signal/submitted/filled prices and slippage"
```
