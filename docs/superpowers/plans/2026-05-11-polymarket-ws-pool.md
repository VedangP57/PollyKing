# Polymarket WebSocket Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Gamma REST poller (5s latency) in `rust-core/src/fetcher/polymarket.rs` with a multi-connection CLOB WebSocket pool (~50ms latency) that scales to 5000+ tokens across N independent `tokio` connections.

**Architecture:** The new `polymarket.rs` maintains N persistent WS connections (one per 500-token chunk) to `wss://ws-subscriptions-clob.polymarket.com/ws/market`, subscribed to `best_bid_ask` events. It starts with a one-shot Gamma REST warm-up, then switches to WS-only. A background task restores stale prices (silent > 120s) via REST. The public `run()` signature is identical to the old one — `main.rs` is untouched.

**Tech Stack:** Rust, tokio, tokio-tungstenite, futures-util, reqwest, serde_json, chrono (all already in Cargo.toml)

---

## File Map

| File | Action | What changes |
|---|---|---|
| `rust-core/src/fetcher/polymarket.rs` | **Rewrite** | REST polling loop → WS pool + stale task |
| `rust-core/src/main.rs` | **No change** | `run()` signature is identical |
| `rust-core/Cargo.toml` | **No change** | All required deps already present |

---

### Task 1: Write failing unit tests for pure helper functions

**Files:**
- Modify: `rust-core/src/fetcher/polymarket.rs` (add stub functions + test module)

These tests document exact behaviour before any implementation. The file will compile but tests will fail until Task 2 fills in the implementations.

- [ ] **Step 1: Replace `polymarket.rs` with a stub + test-only file**

```rust
// rust-core/src/fetcher/polymarket.rs
use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use anyhow::Result;
use chrono::Utc;
use futures_util::{SinkExt, StreamExt};
use log::{info, warn};
use serde_json::Value;
use tokio::sync::{mpsc, watch};
use tokio_tungstenite::{connect_async, tungstenite::Message};

use crate::types::{Platform, Price};

const WS_URL: &str = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
pub const CHUNK_SIZE: usize = 500;
const HEARTBEAT_SECS: u64 = 10;
const STALE_CHECK_SECS: u64 = 60;
const STALE_THRESHOLD_SECS: i64 = 120;
const REST_BATCH_SIZE: usize = 100;

// Stubs — filled in Task 2
pub fn build_subscription_message(_token_ids: &[String]) -> String { todo!() }
pub fn build_dynamic_subscribe_message(_token_ids: &[String]) -> String { todo!() }
pub fn handle_price_message(
    _text: &str,
    _price_map: &Arc<RwLock<HashMap<String, Price>>>,
    _price_watch_tx: &Arc<watch::Sender<u64>>,
) { todo!() }

pub async fn run(
    _gamma_url: String,
    _token_to_gamma: HashMap<String, String>,
    _price_map: Arc<RwLock<HashMap<String, Price>>>,
    _price_watch_tx: Arc<watch::Sender<u64>>,
) -> Result<()> { todo!() }

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::sync::{Arc, RwLock};
    use tokio::sync::watch;

    fn make_price_map() -> Arc<RwLock<HashMap<String, Price>>> {
        Arc::new(RwLock::new(HashMap::new()))
    }
    fn make_watch() -> Arc<watch::Sender<u64>> {
        Arc::new(watch::channel(0u64).0)
    }

    #[test]
    fn test_best_bid_ask_updates_price_map() {
        let pm = make_price_map();
        let tx = make_watch();
        let msg = r#"{"event_type":"best_bid_ask","asset_id":"abc123","bid_price":"0.64","bid_size":"100","ask_price":"0.65","ask_size":"50"}"#;
        handle_price_message(msg, &pm, &tx);
        let map = pm.read().unwrap();
        let price = map.get("poly:abc123").expect("price should be set");
        assert!((price.yes_price - 0.64).abs() < 0.001, "yes={}", price.yes_price);
        assert!((price.no_price - 0.36).abs() < 0.001, "no={}", price.no_price);
    }

    #[test]
    fn test_unknown_event_type_ignored() {
        let pm = make_price_map();
        let tx = make_watch();
        handle_price_message(r#"{"event_type":"tick_size_change","asset_id":"x"}"#, &pm, &tx);
        assert!(pm.read().unwrap().get("poly:x").is_none());
    }

    #[test]
    fn test_zero_bid_price_ignored() {
        let pm = make_price_map();
        let tx = make_watch();
        let msg = r#"{"event_type":"best_bid_ask","asset_id":"abc","bid_price":"0.0","bid_size":"0","ask_price":"0.01","ask_size":"100"}"#;
        handle_price_message(msg, &pm, &tx);
        assert!(pm.read().unwrap().get("poly:abc").is_none(), "zero bid should not update");
    }

    #[test]
    fn test_pong_ignored() {
        let pm = make_price_map();
        let tx = make_watch();
        handle_price_message("PONG", &pm, &tx);
        assert!(pm.read().unwrap().is_empty());
    }

    #[test]
    fn test_array_message_updates_multiple() {
        let pm = make_price_map();
        let tx = make_watch();
        let msg = r#"[
            {"event_type":"best_bid_ask","asset_id":"tok1","bid_price":"0.60","bid_size":"10","ask_price":"0.61","ask_size":"10"},
            {"event_type":"best_bid_ask","asset_id":"tok2","bid_price":"0.40","bid_size":"10","ask_price":"0.41","ask_size":"10"}
        ]"#;
        handle_price_message(msg, &pm, &tx);
        let map = pm.read().unwrap();
        assert!((map["poly:tok1"].yes_price - 0.60).abs() < 0.001);
        assert!((map["poly:tok2"].yes_price - 0.40).abs() < 0.001);
    }

    #[test]
    fn test_chunk_size_splits_correctly() {
        let tokens: Vec<String> = (0..1001).map(|i| format!("t{i}")).collect();
        let chunks: Vec<Vec<String>> = tokens.chunks(CHUNK_SIZE).map(|c| c.to_vec()).collect();
        assert_eq!(chunks.len(), 3, "1001 tokens → 3 chunks");
        assert_eq!(chunks[0].len(), 500);
        assert_eq!(chunks[1].len(), 500);
        assert_eq!(chunks[2].len(), 1);
    }

    #[test]
    fn test_subscription_message_format() {
        let tokens = vec!["tok1".to_string(), "tok2".to_string()];
        let msg = build_subscription_message(&tokens);
        let parsed: serde_json::Value = serde_json::from_str(&msg).unwrap();
        assert_eq!(parsed["type"], "market");
        assert_eq!(parsed["custom_feature_enabled"], true);
        let ids = parsed["assets_ids"].as_array().unwrap();
        assert_eq!(ids.len(), 2);
        assert_eq!(ids[0].as_str().unwrap(), "tok1");
    }

    #[test]
    fn test_dynamic_subscribe_message_format() {
        let tokens = vec!["new_tok".to_string()];
        let msg = build_dynamic_subscribe_message(&tokens);
        let parsed: serde_json::Value = serde_json::from_str(&msg).unwrap();
        assert_eq!(parsed["operation"], "subscribe");
        assert_eq!(parsed["custom_feature_enabled"], true);
        assert_eq!(parsed["assets_ids"][0].as_str().unwrap(), "new_tok");
    }

    #[test]
    fn test_watch_fires_on_price_update() {
        let pm = make_price_map();
        let (tx, mut rx) = watch::channel(0u64);
        let tx = Arc::new(tx);
        let msg = r#"{"event_type":"best_bid_ask","asset_id":"abc","bid_price":"0.55","bid_size":"10","ask_price":"0.56","ask_size":"5"}"#;
        handle_price_message(msg, &pm, &tx);
        assert!(rx.has_changed().unwrap(), "watch must fire on price update");
    }
}
```

- [ ] **Step 2: Run tests — expect compile error on `todo!()`**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/rust-core
cargo test 2>&1 | grep -E "^error|FAILED|panicked|todo"
```

Expected: tests panic with `not yet implemented` (the `todo!()` macro). That confirms the tests exist and the stubs are wired.

---

### Task 2: Implement helper functions — pass the 8 unit tests

**Files:**
- Modify: `rust-core/src/fetcher/polymarket.rs` (replace stubs for the 3 helper functions)

- [ ] **Step 1: Replace the 3 stub functions with real implementations**

Replace the three `todo!()` stubs (leave `run()` as `todo!()` for now):

```rust
pub fn build_subscription_message(token_ids: &[String]) -> String {
    serde_json::json!({
        "assets_ids": token_ids,
        "type": "market",
        "custom_feature_enabled": true,
    })
    .to_string()
}

pub fn build_dynamic_subscribe_message(token_ids: &[String]) -> String {
    serde_json::json!({
        "assets_ids": token_ids,
        "operation": "subscribe",
        "custom_feature_enabled": true,
    })
    .to_string()
}

pub fn handle_price_message(
    text: &str,
    price_map: &Arc<RwLock<HashMap<String, Price>>>,
    price_watch_tx: &Arc<watch::Sender<u64>>,
) {
    if text == "PONG" {
        return;
    }

    let val: Value = match serde_json::from_str(text) {
        Ok(v) => v,
        Err(_) => return,
    };

    // Polymarket can send either a single object or a JSON array of events.
    let messages: Vec<Value> = if val.is_array() {
        val.as_array().cloned().unwrap_or_default()
    } else {
        vec![val]
    };

    let mut updated = false;

    for msg in &messages {
        if msg.get("event_type").and_then(|v| v.as_str()) != Some("best_bid_ask") {
            continue;
        }
        let asset_id = match msg.get("asset_id").and_then(|v| v.as_str()) {
            Some(id) if !id.is_empty() => id,
            _ => continue,
        };
        let bid_price: f64 = match msg
            .get("bid_price")
            .and_then(|v| v.as_str())
            .and_then(|s| s.parse().ok())
        {
            Some(p) if p > 0.0 => p,
            _ => continue,
        };

        price_map.write().unwrap().insert(
            format!("poly:{asset_id}"),
            Price {
                market_id: asset_id.to_string(),
                platform: Platform::Polymarket,
                yes_price: bid_price,
                no_price: 1.0 - bid_price,
                timestamp: Utc::now(),
            },
        );
        updated = true;
    }

    if updated {
        let _ = price_watch_tx.send(*price_watch_tx.borrow() + 1);
    }
}
```

- [ ] **Step 2: Run the 8 unit tests — all must pass**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/rust-core
cargo test fetcher::polymarket 2>&1 | tail -15
```

Expected output:
```
running 8 tests
test fetcher::polymarket::tests::test_array_message_updates_multiple ... ok
test fetcher::polymarket::tests::test_best_bid_ask_updates_price_map ... ok
test fetcher::polymarket::tests::test_chunk_size_splits_correctly ... ok
test fetcher::polymarket::tests::test_dynamic_subscribe_message_format ... ok
test fetcher::polymarket::tests::test_pong_ignored ... ok
test fetcher::polymarket::tests::test_subscription_message_format ... ok
test fetcher::polymarket::tests::test_unknown_event_type_ignored ... ok
test fetcher::polymarket::tests::test_watch_fires_on_price_update ... ok
test result: ok. 8 passed; 0 failed
```

- [ ] **Step 3: Commit the tests + helper implementations**

```bash
git add rust-core/src/fetcher/polymarket.rs
git commit -m "test(polymarket): add 8 unit tests for WS message helpers + implement helpers"
```

---

### Task 3: Implement `fetch_batch`, `run_ws_session`, and `run_connection`

**Files:**
- Modify: `rust-core/src/fetcher/polymarket.rs` (add three private functions above `run()`)

- [ ] **Step 1: Add `fetch_batch` (REST helper — identical to old implementation, kept for warm-up + stale)**

Add this after the `handle_price_message` function:

```rust
async fn fetch_batch(
    client: &reqwest::Client,
    url: &str,
    gamma_to_token: &HashMap<String, String>,
    price_map: &Arc<RwLock<HashMap<String, Price>>>,
) -> Result<usize> {
    let resp = client.get(url).send().await?;
    if !resp.status().is_success() {
        return Err(anyhow::anyhow!("HTTP {}", resp.status()));
    }
    let markets: Value = resp.json().await?;
    let markets = match markets.as_array() {
        Some(a) => a.clone(),
        None => markets["data"].as_array().cloned().unwrap_or_default(),
    };

    let mut count = 0usize;
    let mut map = price_map.write().unwrap();
    for market in &markets {
        let gamma_id = market["id"].as_str().unwrap_or_default();
        let token_id = match gamma_to_token.get(gamma_id) {
            Some(t) => t,
            None => continue,
        };
        let yes_price: f64 = market["outcomePrices"]
            .as_str()
            .and_then(|s| serde_json::from_str::<Vec<String>>(s).ok())
            .and_then(|v| v.first()?.parse().ok())
            .unwrap_or(0.0);
        if yes_price == 0.0 {
            continue;
        }
        map.insert(
            format!("poly:{token_id}"),
            Price {
                market_id: token_id.clone(),
                platform: Platform::Polymarket,
                yes_price,
                no_price: 1.0 - yes_price,
                timestamp: Utc::now(),
            },
        );
        count += 1;
    }
    Ok(count)
}
```

- [ ] **Step 2: Add `run_ws_session` (single connection lifetime)**

Add after `fetch_batch`:

```rust
/// Runs one WebSocket session until the connection closes or errors.
/// Returns Ok(added_tokens) where added_tokens is any tokens subscribed dynamically.
/// The caller (run_connection) re-subscribes added_tokens on reconnect.
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

    loop {
        tokio::select! {
            msg = ws_stream.next() => match msg {
                Some(Ok(Message::Text(text))) => {
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

- [ ] **Step 3: Add `run_connection` (infinite reconnect loop)**

Add after `run_ws_session`:

```rust
/// Owns one WS connection's lifecycle: initial tokens + dynamic additions + reconnect.
/// Never returns in normal operation. Spawned as a tokio task per chunk.
async fn run_connection(
    initial_tokens: Vec<String>,
    price_map: Arc<RwLock<HashMap<String, Price>>>,
    price_watch_tx: Arc<watch::Sender<u64>>,
    mut sub_rx: mpsc::Receiver<Vec<String>>,
) {
    let mut all_tokens = initial_tokens;
    let mut consecutive_errors: u32 = 0;

    loop {
        match run_ws_session(&all_tokens, &price_map, &price_watch_tx, &mut sub_rx).await {
            Ok(added) => {
                all_tokens.extend(added);
                consecutive_errors = 0;
                info!("Polymarket WS session ended — reconnecting in 5s");
                tokio::time::sleep(Duration::from_secs(5)).await;
            }
            Err(e) => {
                consecutive_errors += 1;
                // Backoff: 5s → 10s → 20s → 40s → 80s → 160s (cap 300s)
                let backoff = std::cmp::min(5 * (1u64 << (consecutive_errors - 1).min(6)), 300);
                if consecutive_errors >= 5 {
                    log::error!(
                        "Polymarket WS: {consecutive_errors} consecutive failures — \
                         retrying in {backoff}s. Last error: {e}"
                    );
                } else {
                    warn!(
                        "Polymarket WS error (attempt {consecutive_errors}): {e} — \
                         retrying in {backoff}s"
                    );
                }
                tokio::time::sleep(Duration::from_secs(backoff)).await;
            }
        }
    }
}
```

- [ ] **Step 4: Verify it compiles (debug build)**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/rust-core
cargo build 2>&1 | grep -E "^error"
```

Expected: no output (zero errors). Warnings about unused `dyn_senders` are fine — `run()` is still `todo!()`.

- [ ] **Step 5: Commit**

```bash
git add rust-core/src/fetcher/polymarket.rs
git commit -m "feat(polymarket): add fetch_batch, run_ws_session, run_connection for WS pool"
```

---

### Task 4: Implement `run()` — pool manager + stale background task

**Files:**
- Modify: `rust-core/src/fetcher/polymarket.rs` (replace `run()` stub)

- [ ] **Step 1: Replace the `run()` stub with the full pool manager**

```rust
pub async fn run(
    gamma_url: String,
    token_to_gamma: HashMap<String, String>,
    price_map: Arc<RwLock<HashMap<String, Price>>>,
    price_watch_tx: Arc<watch::Sender<u64>>,
) -> Result<()> {
    if token_to_gamma.is_empty() {
        info!("Polymarket: no tokens to track — WS pool idle.");
        return Ok(());
    }

    // Invert: token_id → gamma_id becomes gamma_id → token_id (for Gamma REST URLs)
    let gamma_to_token: HashMap<String, String> = token_to_gamma
        .iter()
        .filter(|(tok, gid)| !tok.is_empty() && !gid.is_empty())
        .map(|(tok, gid)| (gid.clone(), tok.clone()))
        .collect();
    let gamma_ids: Vec<String> = gamma_to_token.keys().cloned().collect();

    // ── REST warm-up ────────────────────────────────────────────────────────
    // Pre-populate price_map before the first WS connection arrives.
    // The comparator will have real prices from the moment it starts.
    info!("Polymarket: REST warm-up for {} markets", gamma_ids.len());
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()?;
    for chunk in gamma_ids.chunks(REST_BATCH_SIZE) {
        let query = chunk
            .iter()
            .map(|id| format!("id={id}"))
            .collect::<Vec<_>>()
            .join("&");
        let url = format!("{}/markets?{query}", gamma_url.trim_end_matches('/'));
        if let Err(e) = fetch_batch(&client, &url, &gamma_to_token, &price_map).await {
            warn!("REST warm-up batch error: {e}");
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    info!("Polymarket REST warm-up complete — starting WS pool");

    // ── WS pool ─────────────────────────────────────────────────────────────
    let token_ids: Vec<String> = token_to_gamma
        .keys()
        .filter(|t| !t.is_empty())
        .cloned()
        .collect();
    let chunks: Vec<Vec<String>> = token_ids.chunks(CHUNK_SIZE).map(|c| c.to_vec()).collect();
    info!(
        "Polymarket WS pool: {} connections for {} tokens",
        chunks.len(),
        token_ids.len()
    );

    let mut handles = Vec::with_capacity(chunks.len());
    // dyn_senders kept alive for the lifetime of run() so connections can receive
    // dynamic subscription requests from a future management interface.
    let mut _dyn_senders: Vec<mpsc::Sender<Vec<String>>> = Vec::with_capacity(chunks.len());

    for chunk in chunks {
        let (sub_tx, sub_rx) = mpsc::channel::<Vec<String>>(32);
        _dyn_senders.push(sub_tx);
        let pm = Arc::clone(&price_map);
        let ptx = Arc::clone(&price_watch_tx);
        handles.push(tokio::spawn(async move {
            run_connection(chunk, pm, ptx, sub_rx).await
        }));
    }

    // ── Stale price safety net ───────────────────────────────────────────────
    // Tokens silent for > 120s (quiet markets, missed WS events) get a one-shot
    // Gamma REST refresh. This does NOT revert to continuous polling.
    {
        let stale_pm = Arc::clone(&price_map);
        let stale_g2t = gamma_to_token.clone();
        let stale_url = gamma_url.clone();
        let stale_client = reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .build()?;
        tokio::spawn(async move {
            let mut interval =
                tokio::time::interval(Duration::from_secs(STALE_CHECK_SECS));
            loop {
                interval.tick().await;
                let threshold =
                    Utc::now() - chrono::Duration::seconds(STALE_THRESHOLD_SECS);
                let stale_gids: Vec<String> = {
                    let map = stale_pm.read().unwrap();
                    stale_g2t
                        .iter()
                        .filter(|(tok, _)| {
                            map.get(&format!("poly:{tok}"))
                                .map(|p| p.timestamp < threshold)
                                .unwrap_or(true) // never-updated = stale
                        })
                        .map(|(_, gid)| gid.clone())
                        .collect()
                };
                if stale_gids.is_empty() {
                    continue;
                }
                warn!(
                    "Polymarket stale: {} tokens silent > {}s — REST refresh",
                    stale_gids.len(),
                    STALE_THRESHOLD_SECS
                );
                for chunk in stale_gids.chunks(REST_BATCH_SIZE) {
                    let query = chunk
                        .iter()
                        .map(|id| format!("id={id}"))
                        .collect::<Vec<_>>()
                        .join("&");
                    let url = format!("{}/markets?{query}", stale_url.trim_end_matches('/'));
                    if let Err(e) =
                        fetch_batch(&stale_client, &url, &stale_g2t, &stale_pm).await
                    {
                        warn!("Stale refresh error: {e}");
                    }
                }
            }
        });
    }

    // run() never returns in normal operation — all connection tasks loop forever.
    for handle in handles {
        let _ = handle.await;
    }

    Ok(())
}
```

- [ ] **Step 2: Verify `main.rs` is untouched — diff should show zero changes**

```bash
git diff rust-core/src/main.rs
```

Expected: empty output. The `run()` signature (`gamma_url, token_to_gamma, price_map, price_watch_tx`) is identical.

- [ ] **Step 3: Run the 8 unit tests — all still pass**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/rust-core
cargo test fetcher::polymarket 2>&1 | tail -12
```

Expected: `test result: ok. 8 passed; 0 failed`

- [ ] **Step 4: Commit**

```bash
git add rust-core/src/fetcher/polymarket.rs
git commit -m "feat(polymarket): implement WS pool manager run() with REST warm-up + stale task"
```

---

### Task 5: Release build + full test suite

**Files:** None modified — verification only

- [ ] **Step 1: Release build**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/rust-core
cargo build --release 2>&1 | grep -E "^error|Finished"
```

Expected:
```
   Finished `release` profile [optimized] target(s) in Xs
```

- [ ] **Step 2: Run all Rust tests**

```bash
cargo test 2>&1 | tail -15
```

Expected:
```
running 16 tests   ← 8 existing + 8 new polymarket tests
test result: ok. 16 passed; 0 failed; 0 ignored
```

- [ ] **Step 3: Run full Python test suite (unchanged — regression check)**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing/python-core
uv run pytest tests/ -q 2>&1 | tail -5
```

Expected:
```
122 passed in Xs
```

- [ ] **Step 4: Final commit**

```bash
cd /Users/sarvadhisolution/Documents/Personal/PolyyKing
git add rust-core/
git commit -m "feat: replace Polymarket REST polling with enterprise WS pool (~50ms latency)

- Multi-connection CLOB WS pool (500 tokens/connection, N connections for full coverage)
- best_bid_ask events replace 5s Gamma REST polling
- REST warm-up at startup + stale-price fallback every 60s
- Per-connection exponential backoff (5s → 300s cap)
- Dynamic subscription support without reconnecting
- main.rs unchanged — run() signature identical
- 8 new Rust unit tests, all existing tests pass"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| WS URL `wss://ws-subscriptions-clob.polymarket.com/ws/market` | Task 3, Step 2 (WS_URL const) |
| `best_bid_ask` with `custom_feature_enabled: true` | Task 2 (handle_price_message) + Task 3 (run_ws_session) |
| 10s PING heartbeat | Task 3, Step 2 |
| Chunk 500 tokens per connection | Task 3, Step 3 / Task 4 |
| REST warm-up at startup | Task 4, Step 1 |
| Exponential backoff 5s → 300s | Task 3, Step 3 |
| Dynamic subscribe without reconnect | Task 3, Step 3 (sub_rx branch) |
| Stale price background task (60s interval, 120s threshold) | Task 4, Step 1 |
| `run()` signature unchanged, `main.rs` untouched | Task 4, Step 2 |
| 8 unit tests | Task 1, Task 2 |
| Release build passes | Task 5, Step 1 |
| Existing 122 Python tests pass | Task 5, Step 3 |

All spec requirements covered. No gaps.

**Placeholder scan:** No TBDs, no "handle appropriately", no "similar to above". All code blocks are complete.

**Type consistency:** `Price`, `Platform`, `HashMap<String, String>`, `Arc<RwLock<...>>`, `Arc<watch::Sender<u64>>` — all match `types.rs` definitions throughout. `fetch_batch` signature consistent between Task 3 definition and Task 4 call sites.
