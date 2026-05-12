# Rust Pricing Accuracy — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three Rust-layer pricing bugs that cause the gap detector to overstate every arbitrage opportunity by 1–4¢: (1) `apply_delta` never clears a consumed best bid, leaving Kalshi prices stale indefinitely; (2) the comparator uses bid prices where execution needs ask prices; (3) the Polymarket WS handler discards `ask_price` from the feed.

**Architecture:** All changes are in `rust-core`. No Python or DB schema changes. A new per-ticker `KalshiBook` (BTreeMap orderbook) replaces the raise-only delta logic. A new `yes_ask` field on `Price` carries the true execution price for each platform. The comparator `check_cross_platform` is updated to use ask prices. All changes are backward-compatible with the existing `run()` signatures.

**Tech Stack:** Rust stable, `std::collections::BTreeMap`, no new crate dependencies.

---

## File Map

| File | Action | What changes |
|---|---|---|
| `rust-core/src/types.rs` | Modify | Add `yes_ask: f64` to `Price`; update all `Price { .. }` constructors |
| `rust-core/src/fetcher/polymarket.rs` | Modify | Parse `ask_price` from WS event; populate `yes_ask` |
| `rust-core/src/fetcher/kalshi.rs` | Modify | Add `KalshiBook` (BTreeMap); refactor `apply_snapshot`/`apply_delta`; compute `yes_ask` from NO side |
| `rust-core/src/comparator.rs` | Modify | Direction 1: `kalshi.yes_ask`; Direction 2: `poly.yes_ask` |

---

### Task 1: Add `yes_ask` to `Price` + update all constructors

**Files:**
- Modify: `rust-core/src/types.rs`
- Modify: `rust-core/src/fetcher/polymarket.rs` (constructor sites)
- Modify: `rust-core/src/fetcher/kalshi.rs` (constructor sites)

`yes_ask` is the execution price for **buying YES**: on Polymarket, the WS `ask_price`; on Kalshi, `(100 - best_NO_bid) / 100`. Defaults to `yes_price` for callers that don't know the ask yet.

- [ ] **Step 1: Add `yes_ask` to `Price` in `types.rs`**

Replace the `Price` struct:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Price {
    pub market_id: String,
    pub platform: Platform,
    pub yes_price: f64,   // best bid for YES (receiving price when selling YES)
    pub yes_ask: f64,     // best ask for YES (execution price when buying YES)
    pub no_price: f64,    // 1.0 - yes_price (derived)
    pub timestamp: DateTime<Utc>,
}
```

- [ ] **Step 2: Fix every `Price { .. }` constructor that omits `yes_ask`**

In `rust-core/src/fetcher/polymarket.rs`, the `handle_price_message` function inserts a `Price`. Add `yes_ask: bid_price` (placeholder — will be corrected in Task 2):

```rust
price_map.write().unwrap().insert(
    format!("poly:{asset_id}"),
    Price {
        market_id: asset_id.to_string(),
        platform: Platform::Polymarket,
        yes_price: bid_price,
        yes_ask: bid_price,   // placeholder until Task 2 parses ask_price
        no_price: 1.0 - bid_price,
        timestamp: Utc::now(),
    },
);
```

In `rust-core/src/fetcher/kalshi.rs`, the `apply_snapshot` function:

```rust
let price = Price {
    market_id: market_id.to_string(),
    platform: Platform::Kalshi,
    yes_price,
    yes_ask: yes_price,   // placeholder until Task 3 adds NO-side tracking
    no_price: 1.0 - yes_price,
    timestamp: Utc::now(),
};
```

Also update `handle_orderbook` in `kalshi.rs`:

```rust
let price = Price {
    market_id: pair.market_id.clone(),
    platform: Platform::Kalshi,
    yes_price,
    yes_ask: yes_price,   // legacy REST helper — best bid only, no NO tracking
    no_price: 1.0 - yes_price,
    timestamp: Utc::now(),
};
```

- [ ] **Step 3: Verify it compiles**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/rust-core
cargo build 2>&1 | grep -E "^error"
```

Expected: no output (zero errors).

- [ ] **Step 4: Run all existing tests — they must still pass**

```bash
cargo test --quiet 2>&1 | tail -10
```

Expected: all passing (8 Rust tests).

- [ ] **Step 5: Commit**

```bash
git add rust-core/src/types.rs rust-core/src/fetcher/polymarket.rs rust-core/src/fetcher/kalshi.rs
git commit -m "refactor: add yes_ask to Price struct — placeholder=yes_price for now"
```

---

### Task 2: Parse `ask_price` from Polymarket WS events

**Files:**
- Modify: `rust-core/src/fetcher/polymarket.rs`

The `best_bid_ask` event contains both `bid_price` and `ask_price`. We currently discard `ask_price`. We need it to compute `yes_ask` (cost to buy YES on Polymarket).

- [ ] **Step 1: Write the failing test**

Add to the `#[cfg(test)] mod tests` block in `polymarket.rs`:

```rust
#[test]
fn test_ask_price_stored_in_yes_ask() {
    let pm = make_price_map();
    let tx = make_watch();
    // bid=0.64, ask=0.65 — yes_ask must be 0.65, not 0.64
    let msg = r#"{"event_type":"best_bid_ask","asset_id":"asktest","bid_price":"0.64","bid_size":"100","ask_price":"0.65","ask_size":"50"}"#;
    handle_price_message(msg, &pm, &tx);
    let map = pm.read().unwrap();
    let price = map.get("poly:asktest").unwrap();
    assert!((price.yes_price - 0.64).abs() < 0.001, "yes_price should be bid=0.64");
    assert!((price.yes_ask - 0.65).abs() < 0.001, "yes_ask should be ask=0.65");
}

#[test]
fn test_ask_price_absent_falls_back_to_bid() {
    // Some events omit ask_price — fall back to bid so yes_ask is never 0.0
    let pm = make_price_map();
    let tx = make_watch();
    let msg = r#"{"event_type":"best_bid_ask","asset_id":"noask","bid_price":"0.55","bid_size":"10"}"#;
    handle_price_message(msg, &pm, &tx);
    let map = pm.read().unwrap();
    let price = map.get("poly:noask").unwrap();
    assert!((price.yes_ask - 0.55).abs() < 0.001, "yes_ask should fall back to bid=0.55");
}
```

- [ ] **Step 2: Run tests — the new ones must fail**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/rust-core
cargo test fetcher::polymarket::tests::test_ask_price 2>&1 | tail -10
```

Expected: `test_ask_price_stored_in_yes_ask` FAILS (yes_ask == 0.64, not 0.65).

- [ ] **Step 3: Implement — parse `ask_price` in `handle_price_message`**

In `handle_price_message`, after extracting `bid_price`, add:

```rust
let ask_price: f64 = msg
    .get("ask_price")
    .and_then(|v| v.as_str())
    .and_then(|s| s.parse().ok())
    .filter(|&p| p > 0.0)
    .unwrap_or(bid_price);   // fall back to bid if ask absent

price_map.write().unwrap().insert(
    format!("poly:{asset_id}"),
    Price {
        market_id: asset_id.to_string(),
        platform: Platform::Polymarket,
        yes_price: bid_price,
        yes_ask: ask_price,
        no_price: 1.0 - bid_price,
        timestamp: Utc::now(),
    },
);
```

- [ ] **Step 4: Run all polymarket tests — all 11 must pass**

```bash
cargo test fetcher::polymarket 2>&1 | tail -12
```

Expected: `test result: ok. 11 passed; 0 failed`

- [ ] **Step 5: Commit**

```bash
git add rust-core/src/fetcher/polymarket.rs
git commit -m "fix: parse ask_price from Polymarket WS best_bid_ask events into yes_ask"
```

---

### Task 3: Fix Kalshi `apply_delta` staleness with BTreeMap orderbooks

**Files:**
- Modify: `rust-core/src/fetcher/kalshi.rs`

The current `apply_delta` keeps a stale best-bid when the current best is lifted. Fix: maintain per-ticker BTreeMap<price_cents, qty> for both YES and NO sides. Recompute best bid/ask from the book on every update. `yes_ask = (100 - best_NO_bid) / 100`.

- [ ] **Step 1: Write failing tests**

Add to the `#[cfg(test)] mod tests` block in `kalshi.rs`:

```rust
#[test]
fn test_apply_delta_consumed_bid_drops_to_next_level() {
    // Snapshot: YES bids at 65 (qty=100) and 64 (qty=200)
    // After consuming all qty at 65, best bid must drop to 64
    let pm: Arc<RwLock<HashMap<String, Price>>> = Arc::new(RwLock::new(HashMap::new()));
    let books: Arc<RwLock<HashMap<String, KalshiBook>>> = Arc::new(RwLock::new(HashMap::new()));
    let yes = [[65i64, 100i64], [64i64, 200i64]];
    let no: [[i64; 2]] = [];
    apply_snapshot("STALE-TEST", &yes, &no, "stale-market", &pm, &books);

    // Consume all qty at 65 (negative delta)
    apply_delta("STALE-TEST", "yes", 65, -100, &pm, &books);

    let map = pm.read().unwrap();
    let price = map.get("kalshi:STALE-TEST").unwrap();
    assert!(
        (price.yes_price - 0.64).abs() < 0.001,
        "yes_price must drop to next level 0.64 after consuming best bid, got {}",
        price.yes_price
    );
}

#[test]
fn test_apply_delta_positive_qty_raises_best_bid() {
    let pm: Arc<RwLock<HashMap<String, Price>>> = Arc::new(RwLock::new(HashMap::new()));
    let books: Arc<RwLock<HashMap<String, KalshiBook>>> = Arc::new(RwLock::new(HashMap::new()));
    let yes = [[65i64, 100i64]];
    let no: [[i64; 2]] = [];
    apply_snapshot("RAISE-TEST", &yes, &no, "raise-market", &pm, &books);

    // New order at 70 cents — should become new best bid
    apply_delta("RAISE-TEST", "yes", 70, 50, &pm, &books);

    let map = pm.read().unwrap();
    let price = map.get("kalshi:RAISE-TEST").unwrap();
    assert!(
        (price.yes_price - 0.70).abs() < 0.001,
        "yes_price must rise to 0.70 after higher bid arrives, got {}",
        price.yes_price
    );
}

#[test]
fn test_yes_ask_computed_from_no_side() {
    // Snapshot: YES best bid=65¢, NO best bid=33¢
    // yes_ask = (100 - 33) / 100 = 0.67
    let pm: Arc<RwLock<HashMap<String, Price>>> = Arc::new(RwLock::new(HashMap::new()));
    let books: Arc<RwLock<HashMap<String, KalshiBook>>> = Arc::new(RwLock::new(HashMap::new()));
    let yes = [[65i64, 100i64]];
    let no = [[33i64, 100i64]];
    apply_snapshot("ASK-TEST", &yes, &no, "ask-market", &pm, &books);

    let map = pm.read().unwrap();
    let price = map.get("kalshi:ASK-TEST").unwrap();
    assert!(
        (price.yes_price - 0.65).abs() < 0.001,
        "yes_price (bid) should be 0.65, got {}",
        price.yes_price
    );
    assert!(
        (price.yes_ask - 0.67).abs() < 0.001,
        "yes_ask should be (100-33)/100=0.67, got {}",
        price.yes_ask
    );
}

#[test]
fn test_no_delta_updates_yes_ask() {
    // Start with NO best bid=33¢, yes_ask=0.67
    // Delta: NO level at 35 added → new yes_ask=(100-35)/100=0.65
    let pm: Arc<RwLock<HashMap<String, Price>>> = Arc::new(RwLock::new(HashMap::new()));
    let books: Arc<RwLock<HashMap<String, KalshiBook>>> = Arc::new(RwLock::new(HashMap::new()));
    let yes = [[65i64, 100i64]];
    let no = [[33i64, 100i64]];
    apply_snapshot("NO-DELTA", &yes, &no, "no-delta-mkt", &pm, &books);

    apply_delta("NO-DELTA", "no", 35, 50, &pm, &books);

    let map = pm.read().unwrap();
    let price = map.get("kalshi:NO-DELTA").unwrap();
    assert!(
        (price.yes_ask - 0.65).abs() < 0.001,
        "yes_ask must update when NO side changes, got {}",
        price.yes_ask
    );
}
```

- [ ] **Step 2: Run failing tests — confirm they fail to compile (new params)**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/rust-core
cargo test fetcher::kalshi::tests::test_apply_delta_consumed 2>&1 | grep -E "error|FAILED|panicked" | head -5
```

Expected: compile error — `KalshiBook` not yet defined, `apply_snapshot`/`apply_delta` signatures changed.

- [ ] **Step 3: Add `KalshiBook` and refactor `apply_snapshot` + `apply_delta`**

Replace the entire `apply_snapshot` and `apply_delta` functions in `kalshi.rs` with this implementation. Add the `KalshiBook` struct and a module-level `books_map` argument threading:

```rust
use std::collections::BTreeMap;

// Per-ticker orderbook — separate from price_map to avoid bloating Price struct.
#[derive(Debug, Default)]
pub struct KalshiBook {
    yes: BTreeMap<i64, i64>,  // price_cents → qty (qty=0 entries retained until explicitly removed)
    no: BTreeMap<i64, i64>,
}

impl KalshiBook {
    pub fn best_yes_bid_cents(&self) -> Option<i64> {
        self.yes.iter().rev().find(|(_, &qty)| qty > 0).map(|(&p, _)| p)
    }

    pub fn best_no_bid_cents(&self) -> Option<i64> {
        self.no.iter().rev().find(|(_, &qty)| qty > 0).map(|(&p, _)| p)
    }

    pub fn yes_ask_cents(&self) -> Option<i64> {
        // YES ask = 100 - best NO bid (binary market identity)
        self.best_no_bid_cents().map(|no_bid| 100 - no_bid)
    }
}

/// Apply a full orderbook snapshot. Rebuilds the book from scratch and recomputes Price.
fn apply_snapshot(
    ticker: &str,
    yes_levels: &[[i64; 2]],
    no_levels: &[[i64; 2]],
    market_id: &str,
    price_map: &Arc<RwLock<HashMap<String, Price>>>,
    books: &Arc<RwLock<HashMap<String, KalshiBook>>>,
) {
    let mut book = KalshiBook::default();
    for lvl in yes_levels {
        book.yes.insert(lvl[0], lvl[1]);
    }
    for lvl in no_levels {
        book.no.insert(lvl[0], lvl[1]);
    }

    let yes_bid_cents = match book.best_yes_bid_cents() {
        Some(c) => c,
        None => {
            books.write().unwrap().insert(ticker.to_string(), book);
            return;
        }
    };
    let yes_bid = yes_bid_cents as f64 / 100.0;
    let yes_ask = book
        .yes_ask_cents()
        .map(|c| c as f64 / 100.0)
        .unwrap_or(yes_bid);  // fall back to bid when NO side absent

    let price = Price {
        market_id: market_id.to_string(),
        platform: Platform::Kalshi,
        yes_price: yes_bid,
        yes_ask,
        no_price: 1.0 - yes_bid,
        timestamp: Utc::now(),
    };
    price_map.write().unwrap().insert(format!("kalshi:{ticker}"), price);
    books.write().unwrap().insert(ticker.to_string(), book);
}

/// Apply an orderbook delta — update book for YES or NO side, recompute Price.
fn apply_delta(
    ticker: &str,
    side: &str,
    price_cents: i64,
    delta_qty: i64,
    price_map: &Arc<RwLock<HashMap<String, Price>>>,
    books: &Arc<RwLock<HashMap<String, KalshiBook>>>,
) {
    let key = format!("kalshi:{ticker}");
    let mut books_map = books.write().unwrap();
    let book = match books_map.get_mut(ticker) {
        Some(b) => b,
        None => return,  // delta before snapshot — ignore
    };

    // Update the correct side
    let level = match side {
        "yes" => book.yes.entry(price_cents).or_insert(0),
        "no" => book.no.entry(price_cents).or_insert(0),
        _ => return,
    };
    *level = (*level + delta_qty).max(0);

    // Recompute best bid/ask from the updated book
    let yes_bid_cents = match book.best_yes_bid_cents() {
        Some(c) => c,
        None => return,  // book empty — leave last price in price_map
    };
    let yes_bid = yes_bid_cents as f64 / 100.0;
    let yes_ask = book
        .yes_ask_cents()
        .map(|c| c as f64 / 100.0)
        .unwrap_or(yes_bid);

    drop(books_map);  // release books write lock before taking price_map write lock

    let mut map = price_map.write().unwrap();
    if let Some(existing) = map.get_mut(&key) {
        existing.yes_price = yes_bid;
        existing.yes_ask = yes_ask;
        existing.no_price = 1.0 - yes_bid;
        existing.timestamp = Utc::now();
    }
}
```

- [ ] **Step 4: Thread `books` through `handle_ws_message` and `run_ws_session`**

`handle_ws_message` must accept the books map:

```rust
fn handle_ws_message(
    envelope: &WsEnvelope,
    ticker_to_market_id: &HashMap<String, String>,
    price_map: &Arc<RwLock<HashMap<String, Price>>>,
    price_watch_tx: &Arc<watch::Sender<u64>>,
    books: &Arc<RwLock<HashMap<String, KalshiBook>>>,
) {
    // ... existing code, but pass books to apply_snapshot/apply_delta:
    //   apply_snapshot(ticker, &yes, &no, market_id, price_map, books);
    //   apply_delta(&delta.market_ticker, &delta.side, delta.price, delta.delta, price_map, books);
}
```

`run_ws_session` creates `books` once and passes it through:

```rust
async fn run_ws_session(
    api_url: &str,
    api_key: &str,
    api_secret: &str,
    tickers: &[String],
    ticker_to_market_id: &HashMap<String, String>,
    price_map: &Arc<RwLock<HashMap<String, Price>>>,
    price_watch_tx: &Arc<watch::Sender<u64>>,
    books: &Arc<RwLock<HashMap<String, KalshiBook>>>,
) -> anyhow::Result<()> { ... }
```

`run()` creates `books` and passes it to `run_ws_session`:

```rust
let books: Arc<RwLock<HashMap<String, KalshiBook>>> =
    Arc::new(RwLock::new(HashMap::new()));
```

- [ ] **Step 5: Run all Kalshi tests — all must pass**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/rust-core
cargo test fetcher::kalshi 2>&1 | tail -15
```

Expected:
```
running 6 tests
test fetcher::kalshi::tests::test_apply_snapshot_updates_price_map ... ok
test fetcher::kalshi::tests::test_apply_snapshot_ignores_zero_qty ... ok
test fetcher::kalshi::tests::test_apply_delta_consumed_bid_drops_to_next_level ... ok
test fetcher::kalshi::tests::test_apply_delta_positive_qty_raises_best_bid ... ok
test fetcher::kalshi::tests::test_yes_ask_computed_from_no_side ... ok
test fetcher::kalshi::tests::test_no_delta_updates_yes_ask ... ok
test result: ok. 6 passed; 0 failed
```

- [ ] **Step 6: Commit**

```bash
git add rust-core/src/fetcher/kalshi.rs
git commit -m "fix: replace raise-only apply_delta with BTreeMap orderbook — stale best-bid eliminated"
```

---

### Task 4: Fix comparator bid/ask usage

**Files:**
- Modify: `rust-core/src/comparator.rs`

Two pricing errors in the hot loop:
- **Direction 1** (`check_cross_platform` line ~89): uses `kalshi.yes_price` (bid) but execution buys YES → must use `kalshi.yes_ask`
- **Direction 2** (`check_cross_platform` line ~116): uses `poly.yes_price` (bid) but execution buys YES → must use `poly.yes_ask`

- [ ] **Step 1: Write failing tests**

Add a test module to `comparator.rs` (or extend the existing comparator integration tests if present):

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{MarketPair, PairType, Price, Platform};
    use chrono::Utc;
    use crossbeam_channel::unbounded;
    use std::collections::HashMap;

    fn make_price(yes_bid: f64, yes_ask: f64) -> Price {
        Price {
            market_id: "test".into(),
            platform: Platform::Polymarket,
            yes_price: yes_bid,
            yes_ask,
            no_price: 1.0 - yes_bid,
            timestamp: Utc::now(),
        }
    }

    fn make_kalshi_price(yes_bid: f64, yes_ask: f64) -> Price {
        Price {
            market_id: "test".into(),
            platform: Platform::Kalshi,
            yes_price: yes_bid,
            yes_ask,
            no_price: 1.0 - yes_bid,
            timestamp: Utc::now(),
        }
    }

    #[test]
    fn test_dir1_gap_uses_kalshi_yes_ask_not_bid() {
        // Kalshi YES: bid=0.20, ask=0.22
        // Poly NO: 1 - poly_yes_bid = 1 - 0.70 = 0.30
        // With bid (wrong): combined = 0.30 + 0.20 = 0.50 → gap = 50¢ (phantom)
        // With ask (correct): combined = 0.30 + 0.22 = 0.52 → gap = 48¢
        // The gap_cents field in the emitted Gap must reflect the ask price.
        let (gap_tx, gap_rx) = unbounded();
        let config = AppConfig {
            min_gap_cents: 1.0,
            max_gap_cents: 99.0,
            ..AppConfig::from_env().unwrap_or_else(|_| AppConfig {
                dry_run: true,
                min_gap_cents: 1.0, max_gap_cents: 99.0,
                min_bet_usdc: 10.0, max_bet_usdc: 100.0,
                max_daily_loss_usdc: 50.0, max_open_positions: 5,
                polymarket_ws_url: String::new(), polymarket_clob_url: String::new(),
                polymarket_gamma_url: String::new(), kalshi_ws_url: String::new(),
                kalshi_api_url: String::new(), polymarket_api_key: String::new(),
                polymarket_private_key: String::new(), kalshi_api_key: String::new(),
                kalshi_api_secret: String::new(),
            })
        };
        let pair = MarketPair {
            pair_type: PairType::CrossPlatform,
            token_a: "poly_yes".into(),
            no_token_a: "poly_no".into(),
            token_b: "KTEST".into(),
            market_id: "test-mkt".into(),
            gamma_id_a: String::new(),
            gamma_id_b: String::new(),
        };
        let keys = PairKeys {
            key_a: "poly:poly_yes".into(),
            key_b: "kalshi:KTEST".into(),
        };
        let mut map = HashMap::new();
        // Poly YES bid=0.70, ask=0.71 (no_price = 1-0.70 = 0.30)
        map.insert("poly:poly_yes".to_string(), make_price(0.70, 0.71));
        // Kalshi YES bid=0.20, ask=0.22
        map.insert("kalshi:KTEST".to_string(), make_kalshi_price(0.20, 0.22));

        check_cross_platform(&pair, &keys, &map, &config, &gap_tx);

        let gap = gap_rx.try_recv().expect("Gap should be emitted");
        // combined = poly_no + kalshi_yes_ask = 0.30 + 0.22 = 0.52 → gap = 48¢
        assert!(
            (gap.gap_cents - 48.0).abs() < 0.5,
            "Dir1 gap_cents should use kalshi.yes_ask (expected ~48¢), got {}",
            gap.gap_cents
        );
        assert!((gap.kalshi_price - 0.22).abs() < 0.001,
            "kalshi_price in gap should be yes_ask=0.22, got {}", gap.kalshi_price);
    }

    #[test]
    fn test_dir2_gap_uses_poly_yes_ask_not_bid() {
        // Poly YES: bid=0.30, ask=0.31. Kalshi NO: 1 - kalshi_yes_bid = 1 - 0.65 = 0.35
        // With bid (wrong): combined = 0.30 + 0.35 = 0.65 → gap = 35¢ (inflated)
        // With ask (correct): combined = 0.31 + 0.35 = 0.66 → gap = 34¢
        let (gap_tx, gap_rx) = unbounded();
        let config = AppConfig {
            min_gap_cents: 1.0, max_gap_cents: 99.0,
            dry_run: true, min_bet_usdc: 10.0, max_bet_usdc: 100.0,
            max_daily_loss_usdc: 50.0, max_open_positions: 5,
            polymarket_ws_url: String::new(), polymarket_clob_url: String::new(),
            polymarket_gamma_url: String::new(), kalshi_ws_url: String::new(),
            kalshi_api_url: String::new(), polymarket_api_key: String::new(),
            polymarket_private_key: String::new(), kalshi_api_key: String::new(),
            kalshi_api_secret: String::new(),
        };
        let pair = MarketPair {
            pair_type: PairType::CrossPlatform,
            token_a: "poly_yes2".into(),
            no_token_a: String::new(),
            token_b: "KTEST2".into(),
            market_id: "test-mkt2".into(),
            gamma_id_a: String::new(),
            gamma_id_b: String::new(),
        };
        let keys = PairKeys {
            key_a: "poly:poly_yes2".into(),
            key_b: "kalshi:KTEST2".into(),
        };
        let mut map = HashMap::new();
        // Poly YES bid=0.30, ask=0.31
        map.insert("poly:poly_yes2".to_string(), make_price(0.30, 0.31));
        // Kalshi YES bid=0.65, ask=0.67 → no_price = 1-0.65=0.35
        map.insert("kalshi:KTEST2".to_string(), make_kalshi_price(0.65, 0.67));

        check_cross_platform(&pair, &keys, &map, &config, &gap_tx);

        let gap = gap_rx.try_recv().expect("Gap should be emitted");
        // combined = poly_yes_ask + kalshi_no_price = 0.31 + 0.35 = 0.66 → gap=34¢
        assert!(
            (gap.gap_cents - 34.0).abs() < 0.5,
            "Dir2 gap_cents should use poly.yes_ask (expected ~34¢), got {}",
            gap.gap_cents
        );
        assert!((gap.polymarket_price - 0.31).abs() < 0.001,
            "polymarket_price in gap should be yes_ask=0.31, got {}", gap.polymarket_price);
    }
}
```

- [ ] **Step 2: Run tests — both must fail**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/rust-core
cargo test comparator::tests 2>&1 | tail -15
```

Expected: both tests fail (gap_cents reflects bid, not ask).

- [ ] **Step 3: Fix `check_cross_platform` in `comparator.rs`**

Change Direction 1 to use `kalshi.yes_ask`:

```rust
// Direction 1: Buy Poly NO + Buy Kalshi YES
// Use kalshi.yes_ask — execution crosses spread to fill against YES asks.
let combined1 = poly.no_price + kalshi.yes_ask;
let gap1 = (1.0 - combined1) * 100.0;
if gap1 >= config.min_gap_cents && gap1 <= config.max_gap_cents {
    if pair.no_token_a.is_empty() {
        debug!("CrossPlatform dir1 skipped — no_token_a missing for {}", pair.market_id);
    } else {
        let gap = Gap::new(
            "cross_platform".into(),
            pair.market_id.clone(),
            poly.no_price,
            kalshi.yes_ask,   // ← was kalshi.yes_price (bid)
            pair.no_token_a.clone(),
            pair.token_b.clone(),
            "buy".into(),
            gap1,
        );
        let _ = gap_tx.try_send(gap);
    }
}
```

Change Direction 2 to use `poly.yes_ask`:

```rust
// Direction 2: Buy Poly YES + Buy Kalshi NO (= sell Kalshi YES)
// Use poly.yes_ask — execution crosses spread to fill against YES asks on Poly.
let combined2 = poly.yes_ask + kalshi.no_price;
let gap2 = (1.0 - combined2) * 100.0;
if gap2 >= config.min_gap_cents && gap2 <= config.max_gap_cents {
    let gap = Gap::new(
        "cross_platform".into(),
        format!("{}-rev", pair.market_id),
        poly.yes_ask,      // ← was poly.yes_price (bid)
        kalshi.no_price,
        pair.token_a.clone(),
        pair.token_b.clone(),
        "sell".into(),
        gap2,
    );
    let _ = gap_tx.try_send(gap);
}
```

- [ ] **Step 4: Run comparator tests — both must pass**

```bash
cargo test comparator::tests 2>&1 | tail -10
```

Expected: `test result: ok. 2 passed; 0 failed`

- [ ] **Step 5: Commit**

```bash
git add rust-core/src/comparator.rs
git commit -m "fix: comparator uses yes_ask for execution prices — gap no longer overstated by spread"
```

---

### Task 5: Release build + full regression

**Files:** None modified — verification only.

- [ ] **Step 1: Release build**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/rust-core
cargo build --release 2>&1 | grep -E "^error|Finished"
```

Expected: `Finished release profile [optimized] target(s)`

- [ ] **Step 2: Full Rust test suite**

```bash
cargo test --quiet 2>&1 | tail -10
```

Expected: all passing. Count should be ≥ 17 (8 existing Kalshi + 4 new Kalshi + 2 new Comparator + existing Polymarket + others).

- [ ] **Step 3: Full Python test suite (unchanged — regression check)**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/python-core
uv run pytest -q 2>&1 | tail -5
```

Expected: `136 passed`

- [ ] **Step 4: Final summary commit**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
git tag plan-b-complete
```

---

## Self-Review

**Spec coverage:**

| Audit finding | Task |
|---|---|
| `apply_delta` raise-only logic — stale best bid | Task 3 (KalshiBook BTreeMap) |
| NO orderbook ignored in snapshot | Task 3 (`apply_snapshot` tracks both sides) |
| Direction 1 uses bid not ask for Kalshi | Task 4 (`kalshi.yes_ask`) |
| Direction 2 uses bid not ask for Poly | Task 4 (`poly.yes_ask`) |
| Polymarket `ask_price` discarded from WS event | Task 2 |

**Placeholder scan:** No TBDs. All code blocks complete.

**Type consistency:** `Price.yes_ask: f64` introduced in Task 1; populated in Task 2 (Poly) and Task 3 (Kalshi); consumed in Task 4 (comparator). `KalshiBook` defined in Task 3 and used only within `kalshi.rs`. `PairKeys` struct in `comparator.rs` is `pub(crate)` — access from test module requires `#[cfg(test)]` or `pub`.
