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
// Public helpers
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

    // Polymarket sends either a single object or a JSON array of events.
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
        // Evaluate borrow() into a local before send() to release the read lock
        // before send() tries to acquire the write lock on the same RwLock.
        let next = *price_watch_tx.borrow() + 1;
        let _ = price_watch_tx.send(next);
    }
}

// ---------------------------------------------------------------------------
// Private helpers — REST warm-up + stale fallback
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Private helpers — WS session + reconnect loop
// ---------------------------------------------------------------------------

/// Runs one WebSocket session to completion (close or error).
/// Returns the list of dynamically added tokens so the caller can re-subscribe
/// them on the next reconnect.
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

/// Owns one WS connection lifecycle: initial tokens, dynamic additions, reconnect.
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
                info!("Polymarket WS session ended cleanly — reconnecting in 5s");
                tokio::time::sleep(Duration::from_secs(5)).await;
            }
            Err(e) => {
                consecutive_errors += 1;
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

// ---------------------------------------------------------------------------
// Public entry point
// ---------------------------------------------------------------------------

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

    // Invert token_id → gamma_id into gamma_id → token_id for REST URL construction.
    let gamma_to_token: HashMap<String, String> = token_to_gamma
        .iter()
        .filter(|(tok, gid)| !tok.is_empty() && !gid.is_empty())
        .map(|(tok, gid)| (gid.clone(), tok.clone()))
        .collect();
    let gamma_ids: Vec<String> = gamma_to_token.keys().cloned().collect();

    // REST warm-up: pre-populate price_map before the first WS connection arrives.
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

    // Chunk tokens and spawn one persistent connection per chunk.
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
    // _dyn_senders kept alive so connection tasks can receive dynamic subscriptions.
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

    // Stale price safety net: REST-refresh any token silent > STALE_THRESHOLD_SECS.
    {
        let stale_pm = Arc::clone(&price_map);
        let stale_g2t = gamma_to_token.clone();
        let stale_url = gamma_url.clone();
        let stale_client = reqwest::Client::builder()
            .timeout(Duration::from_secs(10))
            .build()?;
        tokio::spawn(async move {
            let mut interval = tokio::time::interval(Duration::from_secs(STALE_CHECK_SECS));
            loop {
                interval.tick().await;
                let threshold =
                    Utc::now() - chrono::Duration::seconds(STALE_THRESHOLD_SECS);
                // stale_g2t: gamma_id → token_id; price_map keys are "poly:{token_id}"
                let stale_gids: Vec<String> = {
                    let map = stale_pm.read().unwrap();
                    stale_g2t
                        .iter()
                        .filter(|(_, token_id)| {
                            map.get(&format!("poly:{token_id}"))
                                .map(|p| p.timestamp < threshold)
                                .unwrap_or(true) // never-updated counts as stale
                        })
                        .map(|(gamma_id, _)| gamma_id.clone())
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
                    let url =
                        format!("{}/markets?{query}", stale_url.trim_end_matches('/'));
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
