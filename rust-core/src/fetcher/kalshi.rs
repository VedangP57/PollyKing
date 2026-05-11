use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, RwLock};

use anyhow::Result;
use base64::Engine;
use chrono::Utc;
use hmac::{Hmac, Mac};
use log::{info, warn};
use serde::Deserialize;
use serde_json::Value;
use sha2::Sha256;
use tokio::sync::watch;

use crate::types::{MarketPair, Platform, Price};

type HmacSha256 = Hmac<Sha256>;

// ---------------------------------------------------------------------------
// WebSocket message types
// ---------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
struct WsEnvelope {
    #[serde(rename = "type")]
    msg_type: String,
    msg: Option<Value>,
}

#[derive(Debug, Deserialize)]
struct OrderbookSnapshotMsg {
    market_ticker: String,
    yes: Option<Vec<[i64; 2]>>,
    no: Option<Vec<[i64; 2]>>,
}

#[derive(Debug, Deserialize)]
struct OrderbookDeltaMsg {
    market_ticker: String,
    side: String,
    price: i64,
    delta: i64,
}

// ---------------------------------------------------------------------------
// Public helpers (kept for executor.rs)
// ---------------------------------------------------------------------------

/// HMAC-SHA256 signing for live order placement (executor.rs).
/// Not used for public market data reads.
pub fn sign_request(api_secret: &str, timestamp: &str, method: &str, path: &str) -> String {
    let message = format!("{timestamp}{method}{path}");
    let mut mac = HmacSha256::new_from_slice(api_secret.as_bytes())
        .expect("HMAC can take any key size");
    mac.update(message.as_bytes());
    base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes())
}

// ---------------------------------------------------------------------------
// Per-ticker orderbook (BTreeMap so best-bid/ask is always O(log n) lookup)
// ---------------------------------------------------------------------------

/// Maintains both YES and NO bid levels for one Kalshi ticker.
/// Stored alongside price_map so deltas can recompute best bid/ask accurately.
#[derive(Debug, Default)]
pub struct KalshiBook {
    yes: BTreeMap<i64, i64>,  // price_cents → qty
    no: BTreeMap<i64, i64>,
}

impl KalshiBook {
    pub fn best_yes_bid_cents(&self) -> Option<i64> {
        self.yes.iter().rev().find(|(_, &qty)| qty > 0).map(|(&p, _)| p)
    }

    pub fn best_no_bid_cents(&self) -> Option<i64> {
        self.no.iter().rev().find(|(_, &qty)| qty > 0).map(|(&p, _)| p)
    }

    /// YES ask = 100 - best NO bid (binary market identity).
    pub fn yes_ask_cents(&self) -> Option<i64> {
        self.best_no_bid_cents().map(|no_bid| 100 - no_bid)
    }

    /// Qty available at the best YES bid.
    pub fn best_yes_bid_qty(&self) -> i64 {
        self.yes.iter().rev().find(|(_, &qty)| qty > 0).map(|(_, &q)| q).unwrap_or(0)
    }

    /// Qty available at the best YES ask (matched against the NO bid book).
    pub fn best_yes_ask_qty(&self) -> i64 {
        self.no.iter().rev().find(|(_, &qty)| qty > 0).map(|(_, &q)| q).unwrap_or(0)
    }
}

// ---------------------------------------------------------------------------
// Price-map helpers
// ---------------------------------------------------------------------------

/// Apply a full orderbook snapshot. Rebuilds the book from scratch, then
/// recomputes yes_bid and yes_ask from both sides. yes/no arrays: [[price_cents, qty], ...].
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
        bid_size: book.best_yes_bid_qty() as f64,
        ask_size: book.best_yes_ask_qty() as f64,
        timestamp: Utc::now(),
    };
    price_map.write().unwrap().insert(format!("kalshi:{ticker}"), price);
    books.write().unwrap().insert(ticker.to_string(), book);
}

/// Apply an orderbook delta — update YES or NO side, recompute best bid/ask from book.
/// Never leaves a stale price when the best bid is consumed.
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

    let level = match side {
        "yes" => book.yes.entry(price_cents).or_insert(0),
        "no"  => book.no.entry(price_cents).or_insert(0),
        _     => return,
    };
    *level = (*level + delta_qty).max(0);

    let yes_bid_cents = match book.best_yes_bid_cents() {
        Some(c) => c,
        None => return,  // book drained — leave last price rather than zeroing
    };
    let yes_bid = yes_bid_cents as f64 / 100.0;
    let yes_ask = book
        .yes_ask_cents()
        .map(|c| c as f64 / 100.0)
        .unwrap_or(yes_bid);

    let bid_size = book.best_yes_bid_qty() as f64;
    let ask_size = book.best_yes_ask_qty() as f64;

    // Release books write lock before acquiring price_map write lock.
    drop(books_map);

    let mut map = price_map.write().unwrap();
    if let Some(existing) = map.get_mut(&key) {
        existing.yes_price = yes_bid;
        existing.yes_ask = yes_ask;
        existing.no_price = 1.0 - yes_bid;
        existing.bid_size = bid_size;
        existing.ask_size = ask_size;
        existing.timestamp = Utc::now();
    }
}

// ---------------------------------------------------------------------------
// WS message dispatcher
// ---------------------------------------------------------------------------

fn handle_ws_message(
    envelope: &WsEnvelope,
    ticker_to_market_id: &HashMap<String, String>,
    price_map: &Arc<RwLock<HashMap<String, Price>>>,
    price_watch_tx: &Arc<watch::Sender<u64>>,
    books: &Arc<RwLock<HashMap<String, KalshiBook>>>,
) {
    let msg_val = match &envelope.msg {
        Some(v) => v,
        None => return,
    };

    let updated = match envelope.msg_type.as_str() {
        "orderbook_snapshot" => {
            if let Ok(snap) =
                serde_json::from_value::<OrderbookSnapshotMsg>(msg_val.clone())
            {
                let ticker = &snap.market_ticker;
                let market_id = ticker_to_market_id
                    .get(ticker)
                    .map(|s| s.as_str())
                    .unwrap_or(ticker.as_str());
                let yes = snap.yes.unwrap_or_default();
                let no = snap.no.unwrap_or_default();
                apply_snapshot(ticker, &yes, &no, market_id, price_map, books);
                true
            } else {
                false
            }
        }
        "orderbook_delta" => {
            if let Ok(delta) =
                serde_json::from_value::<OrderbookDeltaMsg>(msg_val.clone())
            {
                apply_delta(
                    &delta.market_ticker,
                    &delta.side,
                    delta.price,
                    delta.delta,
                    price_map,
                    books,
                );
                true
            } else {
                false
            }
        }
        _ => false,
    };

    if updated {
        let cur = *price_watch_tx.borrow();
        let _ = price_watch_tx.send(cur + 1);
    }
}

// ---------------------------------------------------------------------------
// Single WS session (reconnect loop lives in `run`)
// ---------------------------------------------------------------------------

async fn run_ws_session(
    api_url: &str,
    api_key: &str,
    api_secret: &str,
    tickers: &[String],
    ticker_to_market_id: &HashMap<String, String>,
    price_map: &Arc<RwLock<HashMap<String, Price>>>,
    price_watch_tx: &Arc<watch::Sender<u64>>,
    books: &Arc<RwLock<HashMap<String, KalshiBook>>>,
) -> anyhow::Result<()> {
    use futures_util::{SinkExt, StreamExt};
    use tokio_tungstenite::{connect_async, tungstenite::Message};

    // Derive WS endpoint from the REST base URL.
    let ws_base = api_url
        .replace("https://", "wss://")
        .replace("http://", "ws://");
    let host = ws_base
        .split("/trade-api")
        .next()
        .unwrap_or(&ws_base)
        .trim_end_matches('/');
    let ws_endpoint = format!("{host}/trade-api/ws/v2");

    let timestamp = chrono::Utc::now().timestamp_millis().to_string();
    let signature = sign_request(api_secret, &timestamp, "GET", "/trade-api/ws/v2");

    let request = tokio_tungstenite::tungstenite::handshake::client::Request::builder()
        .uri(&ws_endpoint)
        .header("Kalshi-Access-Key", api_key)
        .header("Kalshi-Access-Signature", &signature)
        .header("Kalshi-Access-Timestamp", &timestamp)
        // Host is set automatically by tungstenite from the URI — do not hardcode.
        .body(())
        .map_err(|e| anyhow::anyhow!("WS request build: {e}"))?;

    let (mut ws_stream, _resp) = connect_async(request)
        .await
        .map_err(|e| anyhow::anyhow!("Kalshi WS connect failed: {e}"))?;

    info!("Kalshi WS connected to {ws_endpoint}");

    // Subscribe to orderbook_delta channel (Kalshi sends initial snapshot + deltas).
    let sub_msg = serde_json::json!({
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta"],
            "market_tickers": tickers,
        }
    });
    ws_stream
        .send(Message::Text(sub_msg.to_string()))
        .await
        .map_err(|e| anyhow::anyhow!("WS subscribe failed: {e}"))?;

    let mut hb = tokio::time::interval(std::time::Duration::from_secs(30));
    hb.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

    loop {
        tokio::select! {
            msg = ws_stream.next() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        if let Ok(env) = serde_json::from_str::<WsEnvelope>(&text) {
                            handle_ws_message(&env, ticker_to_market_id, price_map, price_watch_tx, books);
                        }
                    }
                    Some(Ok(Message::Ping(data))) => {
                        let _ = ws_stream.send(Message::Pong(data)).await;
                    }
                    Some(Ok(Message::Close(_))) | None => {
                        info!("Kalshi WS connection closed");
                        return Ok(());
                    }
                    Some(Err(e)) => {
                        return Err(anyhow::anyhow!("WS read error: {e}"));
                    }
                    Some(Ok(Message::Binary(_))) => {
                        warn!("Kalshi WS: unexpected binary frame — ignoring");
                    }
                    _ => {}
                }
            }
            _ = hb.tick() => {
                let _ = ws_stream.send(Message::Ping(vec![])).await;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

pub async fn run(
    api_url: String,
    api_key: String,
    api_secret: String,
    pairs: Vec<MarketPair>,
    price_map: Arc<RwLock<HashMap<String, Price>>>,
    price_watch_tx: Arc<watch::Sender<u64>>,
) -> Result<()> {
    if pairs.is_empty() {
        info!("Kalshi fetcher: no cross-platform pairs, skipping");
        return Ok(());
    }

    let tickers: Vec<String> = pairs
        .iter()
        .filter(|p| !p.token_b.is_empty())
        .map(|p| p.token_b.clone())
        .collect();

    let ticker_to_market_id: HashMap<String, String> = pairs
        .iter()
        .filter(|p| !p.token_b.is_empty())
        .map(|p| (p.token_b.clone(), p.market_id.clone()))
        .collect();

    info!("Kalshi WS fetcher starting — {} tickers", tickers.len());

    // Per-ticker BTreeMap orderbooks — survive reconnects so we never lose book state.
    let books: Arc<RwLock<HashMap<String, KalshiBook>>> =
        Arc::new(RwLock::new(HashMap::new()));

    // Exponential backoff on consecutive errors: 10s → 20s → 40s → 80s → 160s (cap 300s).
    // A clean close resets the error counter and reconnects after 5s.
    let mut consecutive_errors: u32 = 0;

    loop {
        match run_ws_session(
            &api_url,
            &api_key,
            &api_secret,
            &tickers,
            &ticker_to_market_id,
            &price_map,
            &price_watch_tx,
            &books,
        )
        .await
        {
            Ok(()) => {
                consecutive_errors = 0;
                info!("Kalshi WS session ended — reconnecting in 5s");
                tokio::time::sleep(std::time::Duration::from_secs(5)).await;
            }
            Err(e) => {
                consecutive_errors += 1;
                let backoff_secs = std::cmp::min(10 * (1u64 << (consecutive_errors - 1).min(5)), 300);
                if consecutive_errors >= 5 {
                    log::error!(
                        "Kalshi WS: {consecutive_errors} consecutive failures — \
                         check KALSHI_API_KEY/KALSHI_API_SECRET. Retrying in {backoff_secs}s. \
                         Last error: {e}"
                    );
                } else {
                    warn!("Kalshi WS error (attempt {consecutive_errors}): {e} — retrying in {backoff_secs}s");
                }
                tokio::time::sleep(std::time::Duration::from_secs(backoff_secs)).await;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Legacy REST helper — kept for reference / unit tests
// ---------------------------------------------------------------------------

/// Parse a Kalshi orderbook REST response and update the price map.
///
/// Response shape:
/// ```json
/// { "orderbook": { "yes": [[price_cents, qty], ...], "no": [[price_cents, qty], ...] } }
/// ```
pub fn handle_orderbook(
    text: &str,
    pair: &MarketPair,
    price_map: &Arc<RwLock<HashMap<String, Price>>>,
) -> Result<()> {
    let val: Value = serde_json::from_str(text)?;

    let yes_arr = val
        .get("orderbook")
        .and_then(|ob| ob.get("yes"))
        .and_then(|v| v.as_array());

    let yes_price: f64 = match yes_arr {
        Some(arr) => arr
            .iter()
            .filter_map(|entry| entry.as_array())
            .filter_map(|pair| pair.first()?.as_f64())
            .fold(f64::NEG_INFINITY, f64::max)
            .max(0.0)
            / 100.0,
        None => return Ok(()),
    };

    if yes_price <= 0.0 {
        return Ok(());
    }

    let price = Price {
        market_id: pair.market_id.clone(),
        platform: Platform::Kalshi,
        yes_price,
        yes_ask: yes_price,   // legacy REST helper — best bid only
        no_price: 1.0 - yes_price,
        bid_size: 0.0,
        ask_size: 0.0,
        timestamp: Utc::now(),
    };

    price_map
        .write()
        .unwrap()
        .insert(format!("kalshi:{}", pair.market_id), price);

    Ok(())
}

// ---------------------------------------------------------------------------
// Unit tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn make_books() -> Arc<RwLock<HashMap<String, KalshiBook>>> {
        Arc::new(RwLock::new(HashMap::new()))
    }

    #[test]
    fn test_apply_snapshot_updates_price_map() {
        let pm: Arc<RwLock<HashMap<String, Price>>> = Arc::new(RwLock::new(HashMap::new()));
        let books = make_books();
        let ticker = "TEST-TICKER";
        let market_id = "test-market";
        let yes_levels = [[65i64, 100i64], [60, 200]];
        let no_levels = [[35i64, 100i64]];
        apply_snapshot(ticker, &yes_levels, &no_levels, market_id, &pm, &books);
        let map = pm.read().unwrap();
        let price = map.get(&format!("kalshi:{ticker}")).unwrap();
        assert!(
            (price.yes_price - 0.65).abs() < 0.001,
            "yes_price should be 0.65, got {}",
            price.yes_price
        );
        assert!(
            (price.no_price - 0.35).abs() < 0.001,
            "no_price should be 0.35, got {}",
            price.no_price
        );
    }

    #[test]
    fn test_apply_snapshot_ignores_zero_qty() {
        let pm: Arc<RwLock<HashMap<String, Price>>> = Arc::new(RwLock::new(HashMap::new()));
        let books = make_books();
        let yes_levels = [[65i64, 0i64], [60, 0]];
        apply_snapshot("EMPTY", &yes_levels, &[], "empty-market", &pm, &books);
        let map = pm.read().unwrap();
        assert!(
            map.get("kalshi:EMPTY").is_none(),
            "should not write when all qty=0"
        );
    }

    #[test]
    fn test_apply_delta_consumed_bid_drops_to_next_level() {
        // Snapshot: YES bids at 65 (qty=100) and 64 (qty=200)
        // After consuming all qty at 65, best bid must drop to 64
        let pm: Arc<RwLock<HashMap<String, Price>>> = Arc::new(RwLock::new(HashMap::new()));
        let books = make_books();
        let yes = [[65i64, 100i64], [64i64, 200i64]];
        apply_snapshot("STALE", &yes, &[], "stale-mkt", &pm, &books);

        apply_delta("STALE", "yes", 65, -100, &pm, &books);

        let map = pm.read().unwrap();
        let price = map.get("kalshi:STALE").unwrap();
        assert!(
            (price.yes_price - 0.64).abs() < 0.001,
            "yes_price must drop to next level 0.64, got {}",
            price.yes_price
        );
    }

    #[test]
    fn test_apply_delta_positive_qty_raises_best_bid() {
        let pm: Arc<RwLock<HashMap<String, Price>>> = Arc::new(RwLock::new(HashMap::new()));
        let books = make_books();
        let yes = [[65i64, 100i64]];
        apply_snapshot("RAISE", &yes, &[], "raise-mkt", &pm, &books);

        apply_delta("RAISE", "yes", 70, 50, &pm, &books);

        let map = pm.read().unwrap();
        let price = map.get("kalshi:RAISE").unwrap();
        assert!(
            (price.yes_price - 0.70).abs() < 0.001,
            "yes_price must rise to 0.70, got {}",
            price.yes_price
        );
    }

    #[test]
    fn test_yes_ask_computed_from_no_side() {
        // YES best bid=65¢, NO best bid=33¢ → yes_ask=(100-33)/100=0.67
        let pm: Arc<RwLock<HashMap<String, Price>>> = Arc::new(RwLock::new(HashMap::new()));
        let books = make_books();
        let yes = [[65i64, 100i64]];
        let no = [[33i64, 100i64]];
        apply_snapshot("ASK-TEST", &yes, &no, "ask-mkt", &pm, &books);

        let map = pm.read().unwrap();
        let price = map.get("kalshi:ASK-TEST").unwrap();
        assert!(
            (price.yes_price - 0.65).abs() < 0.001,
            "yes_price (bid) should be 0.65, got {}",
            price.yes_price
        );
        assert!(
            (price.yes_ask - 0.67).abs() < 0.001,
            "yes_ask=(100-33)/100=0.67, got {}",
            price.yes_ask
        );
    }

    #[test]
    fn test_kalshi_snapshot_populates_bid_size() {
        let price_map: Arc<RwLock<HashMap<String, Price>>> = Arc::new(RwLock::new(HashMap::new()));
        let books = make_books();
        let yes = [[60i64, 50i64], [58i64, 30i64]];
        let no  = [[38i64, 20i64]];
        apply_snapshot("TICKER", &yes, &no, "mkt-id", &price_map, &books);
        let map = price_map.read().unwrap();
        let p = map.get("kalshi:TICKER").unwrap();
        assert!((p.bid_size - 50.0).abs() < 0.001, "bid_size should be 50 (qty at best YES bid), got {}", p.bid_size);
        assert!((p.ask_size - 20.0).abs() < 0.001, "ask_size should be 20 (qty at best NO bid → YES ask), got {}", p.ask_size);
    }

    #[test]
    fn test_no_delta_updates_yes_ask() {
        // Start: NO best bid=33¢ → yes_ask=0.67. After delta at NO=35, yes_ask=(100-35)/100=0.65
        let pm: Arc<RwLock<HashMap<String, Price>>> = Arc::new(RwLock::new(HashMap::new()));
        let books = make_books();
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
}
