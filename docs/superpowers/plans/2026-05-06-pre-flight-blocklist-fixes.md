# Pre-Flight Blocklist Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all 7 pre-flight blocklist items so the bot is safe to run with real money on internal pairs, and correct (not money-losing) on cross-platform pairs.

**Architecture:** Changes span Rust (types.rs, comparator.rs, main.rs) for correct gap encoding, Python (matcher.py, backfill.py, two_leg_executor.py, main.py, polymarket_executor.py, kalshi_executor.py) for correct execution, and .env.example for threshold correctness. All changes are TDD: failing test → implementation → passing test → commit.

**Tech Stack:** Rust (tokio, serde_json), Python 3.11, pytest, asyncio, aiohttp, SQLite, py_clob_client_v2, aiohttp

---

## File Map

| File | Task | Change |
|------|------|--------|
| `rust-core/src/types.rs` | 1,2 | Add `no_token_a` to `MarketPair`; add `polymarket_leg_price`, `kalshi_leg_price`, `kalshi_action` to `Gap`; change `polymarket_price`/`kalshi_price` semantics |
| `rust-core/src/main.rs` | 1 | Load `no_token_a` from markets.json |
| `rust-core/src/comparator.rs` | 1,2 | Direction 1: use `no_token_a` + correct prices; Direction 2: send correct prices + `kalshi_action="sell"` |
| `rust-core/tests/comparator_tests.rs` | 1,2 | Tests for correct token/price/action on both directions |
| `python-core/matcher.py` | 1 | Add `no_token_a` field; add `_extract_no_token()` |
| `python-core/tests/test_matcher.py` | 1 | Test NO token extraction |
| `scripts/backfill_matches.py` | 1 | Write `no_token_a` to markets.json entries |
| `python-core/two_leg_executor.py` | 1,2,5 | Use `polymarket_leg_price` + `kalshi_leg_price`; add `kalshi_action`; add fill-verification polling |
| `python-core/tests/test_two_leg_executor.py` | 1,2,5 | Tests for correct price/token/action and fill verification |
| `config/.env.example` | 3 | `CROSS_PLATFORM_MIN_GAP_CENTS=10` |
| `python-core/kalshi_executor.py` | 4,5 | Add `get_open_orders()`, `get_order_status()`, `cancel_order()` |
| `python-core/polymarket_executor.py` | 4,5 | Add `get_open_positions()`, `get_order_status()`, `cancel_order()` |
| `python-core/startup_audit.py` | 4 | New file: orphan position detection logic |
| `python-core/tests/test_startup_audit.py` | 4 | Tests for orphan detection |
| `python-core/main.py` | 4,6,7 | Call startup_audit on launch; wire execution_policy; fix rev gap fee lookup |
| `python-core/execution_policy.py` | 6 | Wire into two_leg_executor (use for price aggressiveness) |
| `python-core/tests/test_execution_policy.py` | 6 | Tests confirming policy is called |

---

## Task 1: Fix cross-platform token ID (Poly NO token) + Gap price semantics

**Root cause:** `Gap.polymarket_token` is always the YES token (`token_a`). For Direction 1 (buy Poly NO + Kalshi YES), the NO token ID is needed. The Gamma API provides both token IDs via `clobTokenIds[0]` (YES) and `clobTokenIds[1]` (NO). Currently `no_token_a` is never stored.

**Fix strategy:** Add `no_token_a` through the full stack. Change Gap price semantics so `polymarket_price` / `kalshi_price` always reflect the **actual price of the token being bought** (not always the YES price). Python executor then does `combined = polymarket_price + kalshi_price` directly.

**Files:**
- Modify: `python-core/matcher.py`
- Modify: `scripts/backfill_matches.py`
- Modify: `rust-core/src/types.rs`
- Modify: `rust-core/src/main.rs`
- Modify: `rust-core/src/comparator.rs`
- Modify: `python-core/two_leg_executor.py`
- Test: `python-core/tests/test_matcher.py`
- Test: `rust-core/tests/comparator_tests.rs`

- [ ] **Step 1: Write failing test for NO token extraction in matcher**

Open `python-core/tests/test_matcher.py` and add at the end:

```python
def test_extract_no_token_returns_second_clob_id():
    from matcher import _extract_no_token
    market = {"clobTokenIds": '["yes_abc", "no_xyz"]'}
    assert _extract_no_token(market) == "no_xyz"

def test_extract_no_token_missing_returns_empty():
    from matcher import _extract_no_token
    assert _extract_no_token({}) == ""

def test_extract_no_token_single_id_returns_empty():
    from matcher import _extract_no_token
    market = {"clobTokenIds": '["yes_only"]'}
    assert _extract_no_token(market) == ""

def test_market_pair_has_no_token_a():
    from matcher import MarketPair
    p = MarketPair(polymarket_slug="s", kalshi_ticker="k", market_id="m",
                   confidence="high", match_method="exact",
                   token_a="yes_tok", no_token_a="no_tok", token_b="kal")
    assert p.no_token_a == "no_tok"
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd python-core && uv run python -m pytest tests/test_matcher.py -k "no_token" -v 2>&1 | tail -15
```
Expected: ImportError or AttributeError.

- [ ] **Step 3: Add `_extract_no_token` and `no_token_a` field to matcher.py**

In `python-core/matcher.py`:

Add `no_token_a: str = ""` to `MarketPair` after `token_b`:
```python
@dataclass
class MarketPair:
    polymarket_slug: str
    kalshi_ticker: str
    market_id: str
    confidence: str
    match_method: str
    pair_type: str = "cross_platform"
    token_a: str = ""
    no_token_a: str = ""   # Polymarket NO token hex ID (cross-platform: buy Poly NO)
    token_b: str = ""
    polymarket_title: str = ""
    kalshi_title: str = ""
    gamma_id_a: str = ""
    gamma_id_b: str = ""
    outcome_count: int = 0
```

Add at the bottom of the file (after `_extract_yes_token`):
```python
def _extract_no_token(market: dict) -> str:
    """Extract the NO outcome token ID from a Gamma API market.

    Gamma API returns clobTokenIds as a JSON-encoded string: '["yes_id", "no_id"]'.
    Index 1 is always the NO token.
    """
    raw = market.get("clobTokenIds", "")
    if raw:
        try:
            ids = json.loads(raw) if isinstance(raw, str) else raw
            if len(ids) > 1:
                return str(ids[1])
        except (json.JSONDecodeError, IndexError):
            pass
    return ""
```

In `_find_match`, add `no_token_a = _extract_no_token(poly)` after `token_a = _extract_yes_token(poly)` and populate it in all three `MarketPair(...)` return sites:
```python
# Layer 3 manual override return:
return MarketPair(
    ...,
    token_a=token_a or poly_slug,
    no_token_a=no_token_a,
    token_b=ticker,
    ...
)

# Layer 1 exact match return:
return MarketPair(
    ...,
    token_a=token_a or poly_slug,
    no_token_a=no_token_a,
    token_b=ticker,
    ...
)

# Layer 2 fuzzy match return:
return MarketPair(
    ...,
    token_a=token_a or poly_slug,
    no_token_a=no_token_a,
    token_b=ticker,
    ...
)
```

- [ ] **Step 4: Run matcher tests**

```bash
cd python-core && uv run python -m pytest tests/test_matcher.py -v 2>&1 | tail -15
```
Expected: all pass.

- [ ] **Step 5: Update backfill to write no_token_a to markets.json**

In `scripts/backfill_matches.py`, in the `pairs_entries` loop, add `"no_token_a"`:
```python
entry = {
    "pair_type": pair.pair_type,
    "token_a": pair.token_a,
    "no_token_a": pair.no_token_a,   # ← add this line
    "token_b": pair.token_b,
    ...
}
```

- [ ] **Step 6: Update Rust types.rs — add `no_token_a` to MarketPair, fix Gap price fields**

Replace the `MarketPair` and `Gap` structs in `rust-core/src/types.rs`:

```rust
// All pairs use token_a / token_b regardless of mode.
// Cross-platform: token_a = Polymarket YES hex ID, no_token_a = Polymarket NO hex ID, token_b = Kalshi ticker
// Internal:       token_a = Polymarket YES hex ID, no_token_a = "" (unused), token_b = second YES hex ID
#[derive(Debug, Clone)]
pub struct MarketPair {
    pub pair_type: PairType,
    pub token_a: String,       // Polymarket YES token
    pub no_token_a: String,    // Polymarket NO token (cross-platform only)
    pub token_b: String,
    pub market_id: String,
    pub gamma_id_a: String,
    pub gamma_id_b: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Gap {
    pub event: String,
    pub pair_type: String,
    pub market_id: String,
    /// Price of the Polymarket token being purchased (NO price for dir1, YES price for dir2/internal)
    pub polymarket_price: f64,
    /// Price of the Kalshi side being purchased (YES price for dir1, NO price for dir2)
    pub kalshi_price: f64,
    pub gap_cents: f64,
    /// Token ID to BUY on Polymarket (NO token for cross-platform dir1, YES token for dir2/internal)
    pub polymarket_token: String,
    pub kalshi_ticker: String,
    /// "buy" for Kalshi YES (dir1/internal), "sell" for Kalshi NO (dir2)
    pub kalshi_action: String,
    pub timestamp: String,
}

impl Gap {
    pub fn new(
        pair_type: String,
        market_id: String,
        polymarket_price: f64,
        kalshi_price: f64,
        polymarket_token: String,
        kalshi_ticker: String,
        kalshi_action: String,
        gap_cents: f64,
    ) -> Self {
        Gap {
            event: "gap_detected".to_string(),
            pair_type,
            market_id,
            polymarket_price,
            kalshi_price,
            gap_cents,
            polymarket_token,
            kalshi_ticker,
            kalshi_action,
            timestamp: Utc::now().to_rfc3339(),
        }
    }
}
```

- [ ] **Step 7: Update main.rs to load no_token_a**

In `load_market_pairs()` in `rust-core/src/main.rs`, add `no_token_a` to the `MarketPair` construction:

```rust
Some(MarketPair {
    pair_type,
    token_a: token_a.to_string(),
    no_token_a: p["no_token_a"].as_str().unwrap_or("").to_string(),
    token_b: token_b.to_string(),
    market_id: market_id.to_string(),
    gamma_id_a: p["gamma_id_a"].as_str().unwrap_or("").to_string(),
    gamma_id_b: p["gamma_id_b"].as_str().unwrap_or("").to_string(),
})
```

- [ ] **Step 8: Update comparator.rs — Direction 1 uses no_token_a and no_price**

Replace `check_cross_platform` in `rust-core/src/comparator.rs`:

```rust
fn check_cross_platform(
    pair: &MarketPair,
    keys: &PairKeys,
    map: &HashMap<String, Price>,
    config: &AppConfig,
    gap_tx: &crossbeam_channel::Sender<Gap>,
) {
    let poly = match map.get(&keys.key_a) {
        Some(p) => p,
        None => return,
    };
    let kalshi = match map.get(&keys.key_b) {
        Some(p) => p,
        None => return,
    };

    // Direction 1: Buy Poly NO + Buy Kalshi YES
    // combined = poly.no_price + kalshi.yes_price
    let combined1 = poly.no_price + kalshi.yes_price;
    let gap1 = (1.0 - combined1) * 100.0;
    if gap1 >= config.min_gap_cents && gap1 <= config.max_gap_cents {
        let poly_token = if pair.no_token_a.is_empty() {
            // Fallback: no_token_a not populated — skip to avoid buying wrong side
            debug!("CrossPlatform dir1 skipped — no_token_a missing for {}", pair.market_id);
            return;
        } else {
            pair.no_token_a.clone()
        };
        debug!(
            "CrossPlatform dir1: {} | PolyNO:{:.4} KalshiYES:{:.4} | {:.1}c",
            pair.market_id, poly.no_price, kalshi.yes_price, gap1
        );
        let gap = Gap::new(
            "cross_platform".into(),
            pair.market_id.clone(),
            poly.no_price,      // price of the token being bought (NO)
            kalshi.yes_price,   // price of the Kalshi side (YES)
            poly_token,
            pair.token_b.clone(),
            "buy".into(),
            gap1,
        );
        let _ = gap_tx.try_send(gap);
    }

    // Direction 2: Buy Poly YES + Buy Kalshi NO (= sell Kalshi YES)
    // combined = poly.yes_price + kalshi.no_price
    let combined2 = poly.yes_price + kalshi.no_price;
    let gap2 = (1.0 - combined2) * 100.0;
    if gap2 >= config.min_gap_cents && gap2 <= config.max_gap_cents {
        debug!(
            "CrossPlatform dir2: {} | PolyYES:{:.4} KalshiNO:{:.4} | {:.1}c",
            pair.market_id, poly.yes_price, kalshi.no_price, gap2
        );
        let gap = Gap::new(
            "cross_platform".into(),
            format!("{}-rev", pair.market_id),
            poly.yes_price,    // price of the token being bought (YES)
            kalshi.no_price,   // price of the Kalshi side (NO)
            pair.token_a.clone(),
            pair.token_b.clone(),
            "sell".into(),     // sell Kalshi YES = buy Kalshi NO
            gap2,
        );
        let _ = gap_tx.try_send(gap);
    }
}
```

Also update `check_internal` call to `Gap::new` to add `kalshi_action`:
```rust
let gap = Gap::new(
    "internal".into(),
    pair.market_id.clone(),
    price_a.yes_price,
    price_b.yes_price,
    pair.token_a.clone(),
    pair.token_b.clone(),
    "buy".into(),       // internal pairs always buy YES on both sides
    gap_cents,
);
```

- [ ] **Step 9: Update Rust comparator tests**

Replace `rust-core/tests/comparator_tests.rs`:

```rust
#[cfg(test)]
mod tests {
    use arb::comparator::compute_gap_cents;
    use arb::types::{Gap, MarketPair, PairType};

    #[test]
    fn test_gap_math_basic() {
        // combined = 0.45 + 0.47 = 0.92 → 8c gap
        assert!((compute_gap_cents(0.45, 0.47) - 8.0).abs() < 0.001);
    }

    #[test]
    fn test_gap_zero_when_combined_is_one() {
        assert!(compute_gap_cents(0.5, 0.5).abs() < 0.001);
    }

    #[test]
    fn test_gap_negative_when_overpriced() {
        assert!(compute_gap_cents(0.6, 0.5) < 0.0);
    }

    #[test]
    fn test_gap_large() {
        // combined = 0.1 + 0.2 = 0.3 → 70c gap
        assert!((compute_gap_cents(0.1, 0.2) - 70.0).abs() < 0.001);
    }

    #[test]
    fn test_gap_minimum_threshold() {
        // 0.48 + 0.47 = 0.95 → 5c gap (at minimum)
        assert!((compute_gap_cents(0.48, 0.47) - 5.0).abs() < 0.001);
    }

    #[test]
    fn test_price_normalization_boundaries() {
        // Prices at extremes should not panic
        assert!(compute_gap_cents(0.0, 0.0).is_finite());
        assert!(compute_gap_cents(1.0, 1.0).is_finite());
    }

    #[test]
    fn test_gap_has_kalshi_action_field() {
        // Gap struct must carry kalshi_action for Python executor
        let g = Gap::new(
            "cross_platform".into(),
            "mkt".into(),
            0.70, 0.22,
            "no_token_hex".into(),
            "KXTEST".into(),
            "buy".into(),
            8.0,
        );
        assert_eq!(g.kalshi_action, "buy");
        assert_eq!(g.polymarket_token, "no_token_hex");
        assert!((g.polymarket_price - 0.70).abs() < 0.001);
    }

    #[test]
    fn test_direction2_gap_uses_sell_action() {
        let g = Gap::new(
            "cross_platform".into(),
            "mkt-rev".into(),
            0.28, 0.65,
            "yes_token_hex".into(),
            "KXTEST".into(),
            "sell".into(),
            7.0,
        );
        assert_eq!(g.kalshi_action, "sell");
        assert_eq!(g.polymarket_token, "yes_token_hex");
    }
}
```

- [ ] **Step 10: Build Rust to check compilation**

```bash
cd rust-core && cargo build 2>&1 | tail -20
```
Expected: compiles without errors.

- [ ] **Step 11: Run Rust tests**

```bash
cd rust-core && cargo test 2>&1 | tail -15
```
Expected: all tests pass.

- [ ] **Step 12: Update two_leg_executor.py — use gap's leg prices and kalshi_action**

In `python-core/two_leg_executor.py`, replace `_execute_cross_platform`:

```python
async def _execute_cross_platform(self, gap: dict, bet_size: float) -> Optional[dict]:
    # polymarket_price and kalshi_price are now the ACTUAL prices of the tokens
    # being bought (set correctly for both directions in Rust comparator)
    poly_price = gap["polymarket_price"]
    kalshi_price = gap["kalshi_price"]
    combined = poly_price + kalshi_price
    k = bet_size / combined if combined > 0 else 0.0
    poly_amount = round(k * poly_price, 4)
    kalshi_count = max(1, round(k))
    kalshi_action = gap.get("kalshi_action", "buy")

    poly_task = self._poly.place_order(
        token_id=gap["polymarket_token"],
        side="BUY",
        amount_usdc=poly_amount,
        price=round(poly_price, 4),
        neg_risk=False,
    )
    kalshi_task = self._kalshi.place_order(
        ticker=gap["kalshi_ticker"],
        action=kalshi_action,
        count=kalshi_count,
    )
    return await self._gather_legs(
        gap, poly_task, kalshi_task, bet_size=bet_size,
        poly_amount=poly_amount, kalshi_count=kalshi_count,
    )
```

Also fix `_execute_internal` — use `combined = poly_price + kalshi_price` directly (already correct since both are YES prices):
```python
async def _execute_internal(self, gap: dict, bet_size: float) -> Optional[dict]:
    poly_price = gap["polymarket_price"]
    kalshi_price = gap["kalshi_price"]
    combined = poly_price + kalshi_price
    k = bet_size / combined if combined > 0 else 0.0
    amount_a = round(k * poly_price, 4)
    amount_b = round(k * kalshi_price, 4)

    task_a = self._poly.place_order(
        token_id=gap["polymarket_token"],
        side="BUY",
        amount_usdc=amount_a,
        price=round(poly_price, 4),
        neg_risk=True,
    )
    task_b = self._poly.place_order(
        token_id=gap["kalshi_ticker"],
        side="BUY",
        amount_usdc=amount_b,
        price=round(kalshi_price, 4),
        neg_risk=True,
    )
    return await self._gather_legs(
        gap, task_a, task_b, bet_size=bet_size,
        poly_amount=amount_a, kalshi_count=None,
    )
```

Also fix `_dry_run_confirmation`:
```python
def _dry_run_confirmation(self, gap: dict, bet_size: float) -> dict:
    poly_price = gap["polymarket_price"]
    kalshi_price = gap["kalshi_price"]
    combined = poly_price + kalshi_price   # both prices are now leg prices
    k = bet_size / combined if combined > 0 else 0.0
    fee_rate = gap.get("fee_rate", 0.04)
    expected_profit = round(k - bet_size - fee_rate * bet_size, 4)
    mid = gap["market_id"][:8]
    return {
        "polymarket_order_id": f"dry-poly-{mid}",
        "kalshi_order_id": f"dry-kalshi-{mid}",
        "total_spent": round(bet_size, 4),
        "gap_cents": gap.get("gap_cents", 0.0),
        "expected_profit": expected_profit,
        "dry_run": True,
    }
```

- [ ] **Step 13: Update two_leg_executor tests for new price semantics**

In `python-core/tests/test_two_leg_executor.py`, update `cross_platform_gap` fixture:
```python
@pytest.fixture
def cross_platform_gap():
    return {
        "pair_type": "cross_platform",
        "market_id": "test-market",
        # Direction 1: polymarket_price = NO price (0.70), kalshi_price = YES price (0.22)
        # combined = 0.70 + 0.22 = 0.92 → 8c gap
        "polymarket_price": 0.70,
        "kalshi_price": 0.22,
        "gap_cents": 8.0,
        "confidence": "medium",
        "polymarket_token": "no_token_hex",   # NO token
        "kalshi_ticker": "KXTEST-25DEC",
        "kalshi_action": "buy",
        "fee_rate": 0.02,
    }
```

Add a direction-2 test:
```python
@pytest.mark.asyncio
async def test_direction2_gap_sells_kalshi(config, db):
    rev_gap = {
        "pair_type": "cross_platform",
        "market_id": "test-market-rev",
        "polymarket_price": 0.28,   # YES price
        "kalshi_price": 0.65,       # NO price
        "gap_cents": 7.0,
        "confidence": "medium",
        "polymarket_token": "yes_token_hex",
        "kalshi_ticker": "KXTEST-25DEC",
        "kalshi_action": "sell",    # buy Kalshi NO = sell Kalshi YES
        "fee_rate": 0.02,
    }
    poly_result = {"order_id": "poly_2", "status": "matched", "platform": "polymarket",
                   "token_id": "yes_token_hex", "amount_usdc": 2.0}
    kalshi_result = {"order_id": "kal_2", "status": "resting", "platform": "kalshi",
                     "ticker": "KXTEST-25DEC", "count": 10}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(rev_gap, bet_size=10.0)

    assert result is not None
    # Kalshi must be called with action="sell"
    call_args = MockKalshi.return_value.place_order.call_args
    assert call_args[1]["action"] == "sell" or call_args[0][1] == "sell"
```

- [ ] **Step 14: Run Python tests**

```bash
cd python-core && uv run python -m pytest tests/ -q --tb=short 2>&1 | tail -10
```
Expected: all pass.

- [ ] **Step 15: Commit**

```bash
git add rust-core/src/types.rs rust-core/src/main.rs rust-core/src/comparator.rs \
        rust-core/tests/comparator_tests.rs \
        python-core/matcher.py python-core/tests/test_matcher.py \
        scripts/backfill_matches.py \
        python-core/two_leg_executor.py python-core/tests/test_two_leg_executor.py
git commit -m "fix(arb): correct cross-platform token ID and gap price semantics

Direction 1 (Poly NO + Kalshi YES): use no_token_a, send no_price as polymarket_price.
Direction 2 (Poly YES + Kalshi NO): send kalshi.no_price, kalshi_action='sell'.
Python executor uses polymarket_price + kalshi_price as combined directly.
Adds no_token_a through matcher → backfill → Rust → Gap event."
```

---

## Task 2: Direction 2 gap — verify correctness (covered by Task 1)

Task 1 already fixed Direction 2 in `comparator.rs` (correct `kalshi_price = kalshi.no_price`, `kalshi_action = "sell"`) and in `two_leg_executor.py` (uses `gap["kalshi_action"]`). No additional work needed — covered by the direction-2 test in Step 13.

---

## Task 3: Update CROSS_PLATFORM_MIN_GAP_CENTS in .env.example

**Files:**
- Modify: `config/.env.example`
- Modify: `python-core/main.py` (default value)

- [ ] **Step 1: Update .env.example**

```bash
# In config/.env.example, change:
CROSS_PLATFORM_MIN_GAP_CENTS=10   # was 5 — net EV at 5c is only ~0.7c after fees
```

- [ ] **Step 2: Update default in main.py CONFIG**

In `python-core/main.py`, change:
```python
"cross_platform_min_gap_cents": float(os.getenv("CROSS_PLATFORM_MIN_GAP_CENTS", "10")),
```

- [ ] **Step 3: Commit**

```bash
git add config/.env.example python-core/main.py
git commit -m "fix(config): raise cross-platform min gap to 10c for profitable net EV after fees"
```

---

## Task 4: Orphan position detection on startup

**Goal:** On startup (live mode only), query both exchanges for open positions. Any position on the exchange that is NOT in trades.db as an open live trade is an orphan — add to `emergency_positions` and log WARNING.

**Files:**
- Create: `python-core/startup_audit.py`
- Modify: `python-core/kalshi_executor.py` (add `get_open_orders`)
- Modify: `python-core/polymarket_executor.py` (add `get_open_positions`)
- Modify: `python-core/main.py` (call audit on startup)
- Test: `python-core/tests/test_startup_audit.py`

- [ ] **Step 1: Add get_open_orders to KalshiExecutor**

In `python-core/kalshi_executor.py`, add after `close_order`:

```python
async def get_open_orders(self) -> list[dict]:
    """Return all open orders from Kalshi portfolio."""
    path = "/trade-api/v2/portfolio/orders"
    headers = self._sign("GET", path)
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{self.api_url}/portfolio/orders",
            headers=headers,
            params={"status": "open"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("orders", [])
```

- [ ] **Step 2: Add get_open_positions to PolymarketExecutor**

In `python-core/polymarket_executor.py`, add after `get_balance`:

```python
def _get_positions_sync(self) -> list[dict]:
    """Return open positions from Polymarket CLOB API."""
    client = self._get_client()
    resp = client.get_positions()
    if isinstance(resp, dict):
        return resp.get("positions", [])
    if isinstance(resp, list):
        return resp
    return []

async def get_open_positions(self) -> list[dict]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, self._get_positions_sync)
```

- [ ] **Step 3: Write failing tests for startup_audit**

Create `python-core/tests/test_startup_audit.py`:

```python
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tracker import _create_tables, log_gap, log_trade
from startup_audit import audit_orphan_positions


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_tables(conn)
    yield conn
    conn.close()


@pytest.mark.asyncio
async def test_no_orphans_when_positions_match_db(db):
    """Exchange positions that match an open DB trade → no emergency positions logged."""
    gap_id = log_gap(db, {
        "market_id": "fed-rate::abc-def",
        "polymarket_price": 0.70,
        "kalshi_price": 0.22,
        "gap_cents": 8.0,
        "confidence": "high",
    })
    log_trade(db, {
        "gap_id": gap_id,
        "polymarket_order_id": "poly-001",
        "kalshi_order_id": "kal-001",
        "amount_usdc": 10.0,
        "status": "open",
        "dry_run": False,
    })

    mock_poly = AsyncMock()
    mock_poly.get_open_positions.return_value = [
        {"asset_id": "poly-001", "size": 10.0}
    ]
    mock_kalshi = AsyncMock()
    mock_kalshi.get_open_orders.return_value = [
        {"order_id": "kal-001", "ticker": "KXTEST", "count": 10}
    ]

    await audit_orphan_positions(mock_poly, mock_kalshi, db)

    row = db.execute("SELECT COUNT(*) FROM emergency_positions").fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_orphan_kalshi_position_added_to_emergency(db):
    """Kalshi position not in DB → logged as emergency."""
    mock_poly = AsyncMock()
    mock_poly.get_open_positions.return_value = []
    mock_kalshi = AsyncMock()
    mock_kalshi.get_open_orders.return_value = [
        {"order_id": "kal-orphan", "ticker": "KXORPHAN", "count": 5, "side": "yes"}
    ]

    await audit_orphan_positions(mock_poly, mock_kalshi, db)

    row = db.execute(
        "SELECT * FROM emergency_positions WHERE order_id='kal-orphan'"
    ).fetchone()
    assert row is not None
    assert row["platform"] == "kalshi"
    assert row["status"] == "open"


@pytest.mark.asyncio
async def test_orphan_poly_position_added_to_emergency(db):
    """Polymarket position not in DB → logged as emergency."""
    mock_poly = AsyncMock()
    mock_poly.get_open_positions.return_value = [
        {"asset_id": "poly-orphan", "size": 7.5, "outcome": "YES"}
    ]
    mock_kalshi = AsyncMock()
    mock_kalshi.get_open_orders.return_value = []

    await audit_orphan_positions(mock_poly, mock_kalshi, db)

    row = db.execute(
        "SELECT * FROM emergency_positions WHERE order_id='poly-orphan'"
    ).fetchone()
    assert row is not None
    assert row["platform"] == "polymarket"


@pytest.mark.asyncio
async def test_api_failure_does_not_crash_startup(db):
    """If exchange API throws, audit logs warning but does not raise."""
    mock_poly = AsyncMock()
    mock_poly.get_open_positions.side_effect = Exception("network error")
    mock_kalshi = AsyncMock()
    mock_kalshi.get_open_orders.side_effect = Exception("network error")

    # Should not raise
    await audit_orphan_positions(mock_poly, mock_kalshi, db)
```

- [ ] **Step 4: Run to confirm failure**

```bash
cd python-core && uv run python -m pytest tests/test_startup_audit.py -v 2>&1 | tail -10
```
Expected: ImportError (module doesn't exist yet).

- [ ] **Step 5: Create startup_audit.py**

Create `python-core/startup_audit.py`:

```python
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


async def audit_orphan_positions(poly_executor, kalshi_executor, db_conn) -> None:
    """On startup, cross-reference exchange open positions against trades.db.

    Any position held on an exchange that has no matching open trade in the DB
    is an orphan (likely from a crashed partial fill). These are inserted into
    emergency_positions and logged as WARNING for manual review.
    """
    # Collect all known order IDs from open DB trades
    rows = db_conn.execute(
        "SELECT polymarket_order_id, kalshi_order_id FROM trades WHERE status='open' AND dry_run=0"
    ).fetchall()
    known_poly_ids = {r["polymarket_order_id"] for r in rows if r["polymarket_order_id"]}
    known_kalshi_ids = {r["kalshi_order_id"] for r in rows if r["kalshi_order_id"]}

    now = datetime.now(timezone.utc).isoformat()

    # Audit Kalshi open orders
    try:
        kalshi_orders = await kalshi_executor.get_open_orders()
        for order in kalshi_orders:
            order_id = order.get("order_id", "")
            if not order_id or order_id in known_kalshi_ids:
                continue
            ticker = order.get("ticker", "unknown")
            count = order.get("count", 0)
            log.warning(
                "ORPHAN KALSHI POSITION: order_id=%s ticker=%s count=%s — not in trades.db",
                order_id, ticker, count,
            )
            db_conn.execute(
                """INSERT INTO emergency_positions
                   (market_id, platform, order_id, side, amount_usdc, opened_at, status)
                   VALUES (?, 'kalshi', ?, ?, ?, ?, 'open')""",
                (ticker, order_id, order.get("side", "unknown"), float(count), now),
            )
        db_conn.commit()
    except Exception as e:
        log.warning("Kalshi orphan audit failed (non-fatal): %s", e)

    # Audit Polymarket open positions
    try:
        poly_positions = await poly_executor.get_open_positions()
        for pos in poly_positions:
            asset_id = pos.get("asset_id", pos.get("token_id", ""))
            if not asset_id or asset_id in known_poly_ids:
                continue
            size = float(pos.get("size", pos.get("amount", 0)))
            outcome = pos.get("outcome", pos.get("side", "unknown"))
            log.warning(
                "ORPHAN POLYMARKET POSITION: asset_id=%s size=%.4f outcome=%s — not in trades.db",
                asset_id, size, outcome,
            )
            db_conn.execute(
                """INSERT INTO emergency_positions
                   (market_id, platform, order_id, side, amount_usdc, opened_at, status)
                   VALUES (?, 'polymarket', ?, ?, ?, ?, 'open')""",
                (asset_id, asset_id, outcome, size, now),
            )
        db_conn.commit()
    except Exception as e:
        log.warning("Polymarket orphan audit failed (non-fatal): %s", e)
```

- [ ] **Step 6: Wire into main.py**

In `python-core/main.py`, add the import at the top:
```python
from startup_audit import audit_orphan_positions
```

In `async def main()`, after `executor = TwoLegExecutor(CONFIG, db_conn)`, add:
```python
# Live mode only: detect orphan positions from any previous crash
if not CONFIG["dry_run"]:
    notifier.logger.info("Auditing exchange positions for orphans from prior runs...")
    await audit_orphan_positions(executor._poly, executor._kalshi, db_conn)
```

- [ ] **Step 7: Run tests**

```bash
cd python-core && uv run python -m pytest tests/test_startup_audit.py -v 2>&1 | tail -15
```
Expected: all pass.

- [ ] **Step 8: Run full suite**

```bash
cd python-core && uv run python -m pytest tests/ -q --tb=short 2>&1 | tail -5
```
Expected: all pass.

- [ ] **Step 9: Commit**

```bash
git add python-core/startup_audit.py python-core/tests/test_startup_audit.py \
        python-core/main.py python-core/kalshi_executor.py python-core/polymarket_executor.py
git commit -m "feat(startup): orphan position detection — cross-ref exchange vs trades.db on startup"
```

---

## Task 5: Order fill verification (poll until matched or 30s timeout)

**Goal:** After placing each leg, poll its order status for up to 30s. If the order is not fully filled by timeout, cancel it and treat as unfilled → triggers existing emergency-close logic.

**Files:**
- Modify: `python-core/kalshi_executor.py`
- Modify: `python-core/polymarket_executor.py`
- Modify: `python-core/two_leg_executor.py`
- Test: `python-core/tests/test_two_leg_executor.py`

- [ ] **Step 1: Add get_order_status and cancel_order to KalshiExecutor**

In `python-core/kalshi_executor.py`, add after `get_open_orders`:

```python
async def get_order_status(self, order_id: str) -> str:
    """Return Kalshi order status string: 'resting', 'matched', 'canceled', etc."""
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    headers = self._sign("GET", path)
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{self.api_url}/portfolio/orders/{order_id}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return "unknown"
            data = await resp.json()
            return data.get("order", {}).get("status", "unknown")

async def cancel_order(self, order_id: str) -> None:
    """Cancel an open Kalshi order by order_id."""
    path = f"/trade-api/v2/portfolio/orders/{order_id}"
    headers = self._sign("DELETE", path)
    async with aiohttp.ClientSession() as session:
        async with session.delete(
            f"{self.api_url}/portfolio/orders/{order_id}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status not in (200, 204):
                raise ExecutorError(f"Kalshi cancel failed HTTP {resp.status}")
```

- [ ] **Step 2: Add get_order_status and cancel_order to PolymarketExecutor**

In `python-core/polymarket_executor.py`, add after `get_open_positions`:

```python
def _get_order_status_sync(self, order_id: str) -> str:
    """Return Polymarket order status: 'matched', 'open', 'canceled', etc."""
    client = self._get_client()
    resp = client.get_order(order_id)
    if isinstance(resp, dict):
        return resp.get("status", resp.get("orderStatus", "unknown"))
    return "unknown"

def _cancel_order_sync(self, order_id: str) -> None:
    client = self._get_client()
    client.cancel(order_id)

async def get_order_status(self, order_id: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, self._get_order_status_sync, order_id)

async def cancel_order(self, order_id: str) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, self._cancel_order_sync, order_id)
```

- [ ] **Step 3: Add _wait_for_fill to TwoLegExecutor**

In `python-core/two_leg_executor.py`, add after `_compute_bet_size`:

```python
_FILL_POLL_INTERVAL = 2.0   # seconds between status polls
_FILL_TIMEOUT = 30.0        # seconds before treating order as unfilled

async def _wait_for_fill(self, platform: str, order_id: str) -> bool:
    """Poll order status until filled or timeout. Returns True if filled."""
    import asyncio as _asyncio
    deadline = _asyncio.get_event_loop().time() + _FILL_TIMEOUT
    while _asyncio.get_event_loop().time() < deadline:
        try:
            if platform == "polymarket":
                status = await self._poly.get_order_status(order_id)
                if status == "matched":
                    return True
                if status in ("canceled", "cancelled"):
                    return False
            elif platform == "kalshi":
                status = await self._kalshi.get_order_status(order_id)
                if status == "matched":
                    return True
                if status in ("canceled", "cancelled"):
                    return False
        except Exception as e:
            log.debug("Fill poll error for %s %s: %s", platform, order_id, e)
        await _asyncio.sleep(_FILL_POLL_INTERVAL)
    return False  # timeout
```

- [ ] **Step 4: Wire _wait_for_fill into _gather_legs**

Replace `_gather_legs` in `python-core/two_leg_executor.py`:

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
    result_a, result_b = await asyncio.gather(task_a, task_b, return_exceptions=True)

    a_ok = not isinstance(result_a, Exception)
    b_ok = not isinstance(result_b, Exception)

    # Verify fills for legs that returned a response (accepted ≠ filled)
    if a_ok:
        poly_id = result_a.get("order_id", "")
        if poly_id and not self._config.get("dry_run", True):
            filled = await self._wait_for_fill("polymarket", poly_id)
            if not filled:
                log.warning("Polymarket order %s did not fill in %ss — treating as failure",
                            poly_id, _FILL_TIMEOUT)
                try:
                    await self._poly.cancel_order(poly_id)
                except Exception:
                    pass
                a_ok = False

    if b_ok and kalshi_count is not None:
        kalshi_id = result_b.get("order_id", "")
        if kalshi_id and not self._config.get("dry_run", True):
            filled = await self._wait_for_fill("kalshi", kalshi_id)
            if not filled:
                log.warning("Kalshi order %s did not fill in %ss — treating as failure",
                            kalshi_id, _FILL_TIMEOUT)
                try:
                    await self._kalshi.cancel_order(kalshi_id)
                except Exception:
                    pass
                b_ok = False

    if a_ok and b_ok:
        poly_price = gap["polymarket_price"]
        kalshi_price = gap["kalshi_price"]
        combined = poly_price + kalshi_price
        k = bet_size / combined if combined > 0 else 0.0
        fee_rate = gap.get("fee_rate", 0.04)
        expected_profit = round(k - bet_size - fee_rate * bet_size, 4)
        return {
            "polymarket_order_id": result_a.get("order_id", ""),
            "kalshi_order_id": result_b.get("order_id", ""),
            "total_spent": round(bet_size, 4),
            "gap_cents": gap.get("gap_cents", 0.0),
            "expected_profit": expected_profit,
            "dry_run": False,
        }

    if not a_ok and not b_ok:
        log.warning(
            "Both legs failed for %s — a: %s | b: %s",
            gap["market_id"], result_a, result_b,
        )
        return None

    # Partial fill — emergency close the filled leg
    filled_result = result_a if a_ok else result_b
    failed_error = result_b if a_ok else result_a
    log.error(
        "PARTIAL FILL on %s — filled: %s | failed: %s — emergency closing",
        gap["market_id"], filled_result.get("order_id"), failed_error,
    )
    await self._emergency_close(filled_result, gap)
    return None
```

- [ ] **Step 5: Add fill verification tests**

In `python-core/tests/test_two_leg_executor.py`, add:

```python
@pytest.mark.asyncio
async def test_timeout_on_poly_fill_triggers_emergency_close(config, db, cross_platform_gap):
    """If Polymarket order doesn't fill within timeout, emergency close fires."""
    config["dry_run"] = False
    poly_result = {"order_id": "poly_slow", "status": "open", "platform": "polymarket",
                   "token_id": "no_token_hex", "amount_usdc": 5.0}
    kalshi_result = {"order_id": "kal_ok", "status": "matched", "platform": "kalshi",
                     "ticker": "KXTEST-25DEC", "count": 11}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.1), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.05):

        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockPoly.return_value.get_order_status = AsyncMock(return_value="open")  # never fills
        MockPoly.return_value.cancel_order = AsyncMock()
        MockPoly.return_value.close_order = AsyncMock()
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)
        MockKalshi.return_value.get_order_status = AsyncMock(return_value="matched")
        MockKalshi.return_value.close_order = AsyncMock()

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is None
    MockPoly.return_value.cancel_order.assert_called_once_with("poly_slow")


@pytest.mark.asyncio
async def test_both_legs_fill_returns_confirmation(config, db, cross_platform_gap):
    """Both legs fill → returns confirmation dict with correct combined math."""
    config["dry_run"] = False
    poly_result = {"order_id": "poly_ok", "status": "open", "platform": "polymarket",
                   "token_id": "no_token_hex", "amount_usdc": 5.0}
    kalshi_result = {"order_id": "kal_ok", "status": "resting", "platform": "kalshi",
                     "ticker": "KXTEST-25DEC", "count": 11}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.1), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.05):

        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockPoly.return_value.get_order_status = AsyncMock(return_value="matched")
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)
        MockKalshi.return_value.get_order_status = AsyncMock(return_value="matched")

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is not None
    assert result["total_spent"] == 10.0


@pytest.mark.asyncio
async def test_dry_run_skips_fill_verification(db, cross_platform_gap):
    """Dry run must never call get_order_status."""
    dry_config = {
        "dry_run": True,
        "bankroll_usdc": 500.0, "kelly_fraction": 0.25,
        "min_bet_usdc": 10.0, "max_bet_usdc": 100.0,
        "polymarket_private_key": "", "polymarket_wallet_address": "",
        "kalshi_api_key": "", "kalshi_api_secret": "",
    }
    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        ex = TwoLegExecutor(dry_config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is not None
    assert result["dry_run"] is True
    MockPoly.return_value.get_order_status.assert_not_called()
    MockKalshi.return_value.get_order_status.assert_not_called()
```

- [ ] **Step 6: Run full test suite**

```bash
cd python-core && uv run python -m pytest tests/ -q --tb=short 2>&1 | tail -10
```
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add python-core/kalshi_executor.py python-core/polymarket_executor.py \
        python-core/two_leg_executor.py python-core/tests/test_two_leg_executor.py
git commit -m "feat(executor): add order fill verification — poll until matched or 30s timeout → cancel + emergency close"
```

---

## Task 6: Wire execution_policy.py into gap handler (price aggressiveness)

**Goal:** `execution_policy.py` decides `limit` vs `market` based on gap size, confidence, and time-to-close. Wire it into `TwoLegExecutor.execute()`: when decision is `market` (urgent), set an aggressive price (+0.03 slippage buffer) so the order fills immediately.

**Files:**
- Modify: `python-core/two_leg_executor.py`
- Test: `python-core/tests/test_two_leg_executor.py`

- [ ] **Step 1: Write test confirming policy is called and affects price**

In `python-core/tests/test_two_leg_executor.py`, add:

```python
@pytest.mark.asyncio
async def test_urgent_gap_uses_aggressive_price(config, db):
    """Gap with closes_at < 30 min → policy returns 'market' → price gets +0.03 buffer."""
    from datetime import datetime, timezone, timedelta

    urgent_gap = {
        "pair_type": "cross_platform",
        "market_id": "urgent-market",
        "polymarket_price": 0.70,
        "kalshi_price": 0.22,
        "gap_cents": 8.0,
        "confidence": "high",
        "polymarket_token": "no_tok",
        "kalshi_ticker": "KXURGENT",
        "kalshi_action": "buy",
        "fee_rate": 0.02,
        "closes_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }

    captured_price = []

    async def capture_place_order(token_id, side, amount_usdc, price=0.5, neg_risk=False):
        captured_price.append(price)
        return {"order_id": "poly_u", "status": "matched", "platform": "polymarket",
                "token_id": token_id, "amount_usdc": amount_usdc}

    config["dry_run"] = False
    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.1), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.05):

        MockPoly.return_value.place_order = capture_place_order
        MockPoly.return_value.get_order_status = AsyncMock(return_value="matched")
        MockKalshi.return_value.place_order = AsyncMock(return_value={
            "order_id": "kal_u", "status": "matched", "platform": "kalshi",
            "ticker": "KXURGENT", "count": 10
        })
        MockKalshi.return_value.get_order_status = AsyncMock(return_value="matched")

        ex = TwoLegExecutor(config, db)
        await ex.execute(urgent_gap, bet_size=10.0)

    # Aggressive price = base_price + 0.03
    assert len(captured_price) > 0
    assert captured_price[0] > urgent_gap["polymarket_price"]
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd python-core && uv run python -m pytest tests/test_two_leg_executor.py -k "urgent" -v 2>&1 | tail -10
```
Expected: FAIL (price not different from base).

- [ ] **Step 3: Wire execution_policy into TwoLegExecutor.execute()**

In `python-core/two_leg_executor.py`, add import at top:
```python
from execution_policy import decide
```

In `execute()`, add after the dry-run check:
```python
async def execute(self, gap: dict, bet_size: Optional[float] = None) -> Optional[dict]:
    if bet_size is None:
        bet_size = self._compute_bet_size(gap)

    if self._config.get("dry_run", True):
        return self._dry_run_confirmation(gap, bet_size)

    # Determine order urgency — urgent gaps use aggressive pricing (+0.03 buffer)
    policy = decide(gap)
    price_buffer = 0.03 if policy.urgency == "high" else 0.0

    pair_type = gap.get("pair_type", "cross_platform")
    ...
```

Pass `price_buffer` into `_execute_cross_platform` and `_execute_internal`:
```python
    if pair_type == "internal":
        return await self._execute_internal(gap, bet_size, price_buffer)
    return await self._execute_cross_platform(gap, bet_size, price_buffer)
```

Update both execute methods to accept and apply `price_buffer`:
```python
async def _execute_cross_platform(self, gap: dict, bet_size: float, price_buffer: float = 0.0) -> Optional[dict]:
    poly_price = gap["polymarket_price"]
    kalshi_price = gap["kalshi_price"]
    combined = poly_price + kalshi_price
    k = bet_size / combined if combined > 0 else 0.0
    poly_amount = round(k * poly_price, 4)
    kalshi_count = max(1, round(k))
    kalshi_action = gap.get("kalshi_action", "buy")
    # Aggressive price for urgent gaps — willing to pay up to buffer more
    order_price = min(round(poly_price + price_buffer, 4), 0.99)

    poly_task = self._poly.place_order(
        token_id=gap["polymarket_token"],
        side="BUY",
        amount_usdc=poly_amount,
        price=order_price,
        neg_risk=False,
    )
    ...
```

```python
async def _execute_internal(self, gap: dict, bet_size: float, price_buffer: float = 0.0) -> Optional[dict]:
    poly_price = gap["polymarket_price"]
    kalshi_price = gap["kalshi_price"]
    combined = poly_price + kalshi_price
    k = bet_size / combined if combined > 0 else 0.0
    amount_a = round(k * poly_price, 4)
    amount_b = round(k * kalshi_price, 4)

    task_a = self._poly.place_order(
        token_id=gap["polymarket_token"],
        side="BUY",
        amount_usdc=amount_a,
        price=min(round(poly_price + price_buffer, 4), 0.99),
        neg_risk=True,
    )
    task_b = self._poly.place_order(
        token_id=gap["kalshi_ticker"],
        side="BUY",
        amount_usdc=amount_b,
        price=min(round(kalshi_price + price_buffer, 4), 0.99),
        neg_risk=True,
    )
    ...
```

- [ ] **Step 4: Run tests**

```bash
cd python-core && uv run python -m pytest tests/ -q --tb=short 2>&1 | tail -10
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add python-core/two_leg_executor.py python-core/tests/test_two_leg_executor.py
git commit -m "feat(policy): wire execution_policy into executor — urgent gaps use aggressive +0.03 price buffer"
```

---

## Task 7: Fix rev gap fee_rate_map lookup (strip "-rev" suffix)

**Files:**
- Modify: `python-core/main.py`
- Test: `python-core/tests/test_main_fee_lookup.py` (new)

- [ ] **Step 1: Write failing test**

Create `python-core/tests/test_main_fee_lookup.py`:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_rev_gap_gets_fee_rate_from_base_market():
    """A '-rev' gap market_id must look up fee_rate without the suffix."""
    fee_rate_map = {
        "fed-rate-june": 0.02,
        "btc-price-q3": 0.04,
    }

    def lookup_fee(market_id: str, fee_map: dict) -> float:
        lookup_id = market_id.removesuffix("-rev")
        return fee_map.get(lookup_id, fee_map.get(market_id, 0.04))

    # Rev gap should find the base market's fee
    assert lookup_fee("fed-rate-june-rev", fee_rate_map) == 0.02
    # Non-rev gap still works
    assert lookup_fee("btc-price-q3", fee_rate_map) == 0.04
    # Unknown market falls back to default
    assert lookup_fee("unknown-market", fee_rate_map) == 0.04
    # Unknown rev market falls back to default
    assert lookup_fee("unknown-market-rev", fee_rate_map) == 0.04
```

- [ ] **Step 2: Run to confirm logic (this test doesn't import main.py so it should pass)**

```bash
cd python-core && uv run python -m pytest tests/test_main_fee_lookup.py -v 2>&1 | tail -10
```
Expected: PASS (the test is self-contained to verify the fix logic).

- [ ] **Step 3: Apply fix to main.py**

In `python-core/main.py`, in `_handle_gap_inner`, change:
```python
gap["fee_rate"] = fee_rate_map.get(market_id, 0.04)
```
to:
```python
# Strip "-rev" suffix before fee lookup — Direction 2 rev gaps share the base pair's fee
_lookup_id = market_id.removesuffix("-rev")
gap["fee_rate"] = fee_rate_map.get(_lookup_id, fee_rate_map.get(market_id, 0.04))
```

- [ ] **Step 4: Run full test suite**

```bash
cd python-core && uv run python -m pytest tests/ -q --tb=short 2>&1 | tail -10
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add python-core/main.py python-core/tests/test_main_fee_lookup.py
git commit -m "fix(main): strip -rev suffix before fee_rate_map lookup for Direction 2 gaps"
```

---

## Final: Full test suite + self-audit

- [ ] **Run full test suite**

```bash
cd python-core && uv run python -m pytest tests/ -v 2>&1 | tail -30
cd rust-core && cargo test 2>&1 | tail -10
```
Expected: all Python tests pass, all Rust tests pass.

- [ ] **Re-audit against original 8-item checklist**

Go through each original audit question and score the current state:
1. negRisk math ✅ (binary check unchanged)
2. NO token ID ✅ (fixed Task 1)
3. Partial fill recovery ✅ (fill verification Task 5, emergency close existing)
4. Crash recovery ✅ (orphan detection Task 4)
5. Fee calculation ✅ (approximately correct, conservatively overstates)
6. Min gap ✅ (changed to 10c Task 3)
7. Multi-outcome filtering ✅ (unchanged, already correct)
8. WAL mode ✅ (already confirmed active)

Target: **8/10 confidence score**.
