# Polymarket WebSocket Pool — Design Spec

**Date:** 2026-05-11
**Status:** Approved
**Goal:** Replace Gamma REST polling (5s latency) with a multi-connection CLOB WebSocket pool (~50ms latency) to match Kalshi's real-time feed quality.

---

## Problem

The current `polymarket.rs` polls `gamma-api.polymarket.com` every 5 seconds in batches.
Kalshi prices arrive via WebSocket in ~50ms. This asymmetry causes the comparator to wake on
a fresh Kalshi price and compare it against a Polymarket price that is 0–5s stale — generating
false gap signals and real execution risk.

The old WebSocket domain (`ws-subscriptions.polymarket.com`) no longer resolves as of 2026-05.
The replacement domain `ws-subscriptions-clob.polymarket.com` is live and public (no auth).

---

## Solution: Approach A — In-process WS Pool (Rust, tokio)

Maintain N persistent WebSocket connections inside the Rust process, each subscribed to ~500
Polymarket token IDs. All connections share the existing `price_map` and `price_watch_tx`.

### Why not Approach B (RTDS)?
RTDS (`wss://ws-live-data.polymarket.com`) message format is not formally documented and
can change without notice. Not a stable foundation.

### Why not Approach C (Redis/NATS broker)?
Single-node deployment. A message broker adds infra cost without a second consumer to justify it.

---

## WebSocket API Contract

| Field | Value |
|---|---|
| Endpoint | `wss://ws-subscriptions-clob.polymarket.com/ws/market` |
| Auth | None (public) |
| Heartbeat | Send `PING` every 10s; server replies `PONG` |
| Subscription | JSON payload on connect |

**Subscription payload:**
```json
{
  "assets_ids": ["<token_id_1>", "<token_id_2>", "...up to 500"],
  "type": "market",
  "custom_feature_enabled": true
}
```

**Dynamic add (no reconnect):**
```json
{
  "assets_ids": ["<new_token_id>"],
  "operation": "subscribe",
  "custom_feature_enabled": true
}
```

**Event used: `best_bid_ask`**
```json
{
  "event_type": "best_bid_ask",
  "asset_id": "<token_id>",
  "bid_price": "0.64",
  "bid_size": "150",
  "ask_price": "0.65",
  "ask_size": "200",
  "timestamp": "..."
}
```

`yes_price = bid_price.parse::<f64>()` — best bid is the highest price a buyer will pay for YES.
`no_price = 1.0 - yes_price` — derived (same convention as current code).

---

## Architecture

```
rust-core/src/fetcher/polymarket.rs   ← complete rewrite
rust-core/src/main.rs                 ← minor: pass token_ids instead of token_to_gamma
```

### Pool Manager — `run()`

Signature (replaces current `run()`):
```rust
pub async fn run(
    gamma_url: String,
    token_to_gamma: HashMap<String, String>,  // token_id → gamma_id (kept for REST warm-up + stale fallback)
    price_map: Arc<RwLock<HashMap<String, Price>>>,
    price_watch_tx: Arc<watch::Sender<u64>>,
) -> Result<()>
```

The `token_to_gamma` map is **not used for WS subscriptions** — WS subscribes by token ID
directly. It is retained only for:
- The one-shot REST warm-up call (Gamma API requires gamma IDs in the URL)
- The stale fallback task (same Gamma REST call for tokens silent > 120s)

Token IDs for WS subscription are extracted from `token_to_gamma.keys()`.

Steps:
1. **REST warm-up**: One Gamma REST pass using `fetch_batch()` to pre-populate `price_map`
   before WS connects. Ensures comparator has prices immediately on startup.
2. **Chunk**: Split token IDs into groups of 500. This is a conservative operational limit,
   not an API-enforced one — reduces blast radius if a connection drops.
3. **Spawn**: One `tokio::spawn` per chunk, each running an infinite reconnect loop around
   `run_ws_session()`. These tasks never exit in normal operation.
4. **Join**: `futures::future::join_all()` awaits all tasks. Because each task runs an
   infinite reconnect loop, `run()` itself never returns — this is intentional and mirrors
   how `kalshi::run()` behaves.

### WS Session — `run_ws_session()`

```rust
async fn run_ws_session(
    chunk: Vec<String>,
    price_map: Arc<RwLock<HashMap<String, Price>>>,
    price_watch_tx: Arc<watch::Sender<u64>>,
    subscribe_rx: mpsc::Receiver<Vec<String>>,   // dynamic subscription channel
) -> Result<()>
```

**Inner reconnect loop** (mirrors `kalshi.rs`):
- `consecutive_errors: u32` counter; backoff = `min(5 * 2^(errors-1), 300)` seconds
- Clean close resets counter; error increments it

**Per-session message loop:**
```
connect → send subscription → loop {
    select! {
        msg = ws.next()    => handle_ws_message()
        _ = hb.tick()      => send PING
        tokens = sub_rx.recv() => send dynamic subscribe payload
    }
}
```

**Message handling (`handle_ws_message()`):**
- `"PONG"` → ignore
- JSON parse → match `event_type`:
  - `"best_bid_ask"` → parse `asset_id`, `bid_price` → update `price_map` → fire `price_watch_tx`
  - `"book"` → parse full snapshot → warm prices (same logic as `best_bid_ask` on best level)
  - `"price_change"` → ignore (superseded by `best_bid_ask`)
  - unknown → ignore

### Dynamic Subscription

Pool manager holds `Vec<mpsc::Sender<Vec<String>>>`, one per connection.
New tokens are round-robin assigned to the connection with fewest current subscriptions.
The session's `select!` loop reads from `subscribe_rx` and sends the subscribe payload.

### Stale Price Safety Net

Separate `tokio::spawn` task:
- Every 60s: scan `price_map` for entries with `timestamp < now - 120s`
- For stale tokens: fire a one-shot Gamma REST fetch (reuse `fetch_batch()`)
- Covers quiet markets and missed events — does not revert to continuous polling

---

## What Changes

| File | Change |
|---|---|
| `rust-core/src/fetcher/polymarket.rs` | Complete rewrite — REST poller → WS pool |
| `rust-core/src/main.rs` | Pass `gamma_url` + `token_to_gamma` to new `run()` — same call site shape, new behaviour |

## What Does NOT Change

| Component | Reason unchanged |
|---|---|
| `comparator.rs` | Already event-driven on `price_watch_tx` |
| `kalshi.rs` | Unrelated |
| `types.rs` | `Price` struct unchanged; `poly:{token_id}` keys unchanged |
| `bridge.rs` | Python↔Rust gap relay unchanged |
| Python executor / detector / reconciler | Unchanged |
| `tracker.py` / DB schema | Gamma IDs stay in `market_pairs` for reconciliation |
| 122 Python tests + 8 Rust tests | All pass unchanged |

---

## Removed

- Continuous REST polling loop removed. REST is warm-up only + stale fallback.
- `token_to_gamma` is retained in `run()` signature for REST use; it is **not passed into WS session logic**.

---

## Error Handling

| Scenario | Behaviour |
|---|---|
| WS connect fails | Exponential backoff per connection (5s → 300s) |
| 5+ consecutive failures | `error!` log with key/secret hint (mirrors Kalshi) |
| Partial pool failure | Other connections unaffected; failed chunk reconnects independently |
| `best_bid_ask` parse error | Log `warn!`, skip message, keep running |
| Price stale > 120s | Background task fires REST refresh for that token |
| All connections dead | Stale prices remain; reconciler + circuit breaker prevent new trades |

---

## Testing

| Test | Type |
|---|---|
| `best_bid_ask` JSON → correct `Price` struct | Unit |
| `book` snapshot → `Price` populated | Unit |
| 5001 tokens → 11 chunks of ≤500 | Unit |
| Dynamic subscribe message format | Unit |
| Stale detector identifies tokens > 120s | Unit |
| Mock WS server: connect → subscribe → emit event → assert price_map | Integration |
| Reconnect on mock server drop → backoff respected | Integration |
| Existing 8 Rust unit tests | Regression |
| Existing 122 Python tests | Regression |

---

## Expected Outcome

| Metric | Before | After |
|---|---|---|
| Polymarket price latency | 0–5000ms | ~50ms |
| Price freshness on comparator wake | Potentially 5s stale | Always fresh |
| False gap signals from stale Poly prices | High risk | Eliminated |
| System rating (price feed) | 4/10 | 9/10 |
| Infrastructure added | None | None |
