use std::collections::HashMap;
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
// Price-map helpers
// ---------------------------------------------------------------------------

/// Apply a full orderbook snapshot. yes/no arrays: [[price_cents, qty], ...].
/// Best bid = highest price with qty > 0.
fn apply_snapshot(
    ticker: &str,
    yes_levels: &[[i64; 2]],
    _no_levels: &[[i64; 2]],
    market_id: &str,
    price_map: &Arc<RwLock<HashMap<String, Price>>>,
) {
    let yes_price = yes_levels
        .iter()
        .filter(|lvl| lvl[1] > 0)
        .map(|lvl| lvl[0])
        .max()
        .unwrap_or(0) as f64
        / 100.0;

    if yes_price <= 0.0 {
        return;
    }

    let price = Price {
        market_id: market_id.to_string(),
        platform: Platform::Kalshi,
        yes_price,
        no_price: 1.0 - yes_price,
        timestamp: Utc::now(),
    };
    price_map
        .write()
        .unwrap()
        .insert(format!("kalshi:{ticker}"), price);
}

/// Apply an orderbook delta — only YES side tracked for pricing.
fn apply_delta(
    ticker: &str,
    side: &str,
    price_cents: i64,
    delta_qty: i64,
    price_map: &Arc<RwLock<HashMap<String, Price>>>,
) {
    if side != "yes" {
        return;
    }
    let key = format!("kalshi:{ticker}");
    let new_yes = price_cents as f64 / 100.0;
    let mut map = price_map.write().unwrap();
    if let Some(existing) = map.get_mut(&key) {
        if delta_qty > 0 && new_yes > existing.yes_price {
            existing.yes_price = new_yes;
            existing.no_price = 1.0 - new_yes;
            existing.timestamp = Utc::now();
        }
        // If delta removes current best bid, keep stale value (safer than zeroing)
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
                apply_snapshot(ticker, &yes, &no, market_id, price_map);
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
                            handle_ws_message(&env, ticker_to_market_id, price_map, price_watch_tx);
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
        no_price: 1.0 - yes_price,
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

    #[test]
    fn test_apply_snapshot_updates_price_map() {
        let pm: Arc<RwLock<HashMap<String, Price>>> =
            Arc::new(RwLock::new(HashMap::new()));
        let ticker = "TEST-TICKER";
        let market_id = "test-market";
        let yes_levels = [[65i64, 100i64], [60, 200]];
        let no_levels = [[35i64, 100i64]];
        apply_snapshot(ticker, &yes_levels, &no_levels, market_id, &pm);
        let map = pm.read().unwrap();
        let price = map.get(&format!("kalshi:{ticker}")).unwrap();
        // best YES bid = 65 cents = 0.65
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
        let pm: Arc<RwLock<HashMap<String, Price>>> =
            Arc::new(RwLock::new(HashMap::new()));
        // All YES levels have qty=0 — nothing should be written
        let yes_levels = [[65i64, 0i64], [60, 0]];
        apply_snapshot("EMPTY", &yes_levels, &[], "empty-market", &pm);
        let map = pm.read().unwrap();
        assert!(
            map.get("kalshi:EMPTY").is_none(),
            "should not write when all qty=0"
        );
    }
}
