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

// ---------------------------------------------------------------------------
// Public helpers — stubs (filled in Task 2)
// ---------------------------------------------------------------------------

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
        let next = *price_watch_tx.borrow() + 1;
        let _ = price_watch_tx.send(next);
    }
}

// ---------------------------------------------------------------------------
// Public entry point — stub (filled in Task 4)
// ---------------------------------------------------------------------------

pub async fn run(
    _gamma_url: String,
    _token_to_gamma: HashMap<String, String>,
    _price_map: Arc<RwLock<HashMap<String, Price>>>,
    _price_watch_tx: Arc<watch::Sender<u64>>,
) -> Result<()> {
    todo!("implement run")
}

// ---------------------------------------------------------------------------
// Unit tests
// ---------------------------------------------------------------------------

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
    fn test_watch_counter_increments_when_price_map_updated() {
        // Keep _rx alive so send() returns Ok(()) and actually updates shared state.
        // borrow() reads the shared state synchronously — no tokio runtime required.
        let pm = make_price_map();
        let (tx_inner, _rx) = watch::channel(0u64);
        let tx = Arc::new(tx_inner);
        let msg = r#"{"event_type":"best_bid_ask","asset_id":"wtest","bid_price":"0.55","bid_size":"10","ask_price":"0.56","ask_size":"5"}"#;
        handle_price_message(msg, &pm, &tx);
        assert_eq!(*tx.borrow(), 1u64, "watch counter must be 1 after one price update");
    }
}
