use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use anyhow::Result;
use base64::Engine;
use chrono::Utc;
use hmac::{Hmac, Mac};
use log::{error, info, warn};
use serde_json::Value;
use sha2::Sha256;
use tokio::sync::Notify;

use crate::types::{MarketPair, Platform, Price};

type HmacSha256 = Hmac<Sha256>;

/// HMAC-SHA256 signing for live order placement (executor.rs).
/// Not used for public market data reads.
pub fn sign_request(api_secret: &str, timestamp: &str, method: &str, path: &str) -> String {
    let message = format!("{timestamp}{method}{path}");
    let mut mac = HmacSha256::new_from_slice(api_secret.as_bytes())
        .expect("HMAC can take any key size");
    mac.update(message.as_bytes());
    base64::engine::general_purpose::STANDARD.encode(mac.finalize().into_bytes())
}

/// Poll Kalshi orderbook prices via the public REST API.
///
/// Public endpoint: https://api.elections.kalshi.com/trade-api/v2
/// No API key required for reading prices — authentication is only needed
/// for live order placement (DRY_RUN=false via executor.rs).
///
/// Polls GET /markets/{ticker}/orderbook every 2 seconds for each tracked ticker.
pub async fn run(
    api_url: String,
    api_key: String,      // optional — only for live trading
    _api_secret: String,
    pairs: Vec<MarketPair>,
    price_map: Arc<RwLock<HashMap<String, Price>>>,
    price_notify: Arc<Notify>,
) -> Result<()> {
    if pairs.is_empty() {
        info!("Kalshi fetcher: no cross-platform pairs configured, skipping");
        return Ok(());
    }

    // Collect (ticker, pair) tuples for polling
    let tracked: Vec<(String, MarketPair)> = pairs
        .into_iter()
        .filter(|p| !p.token_b.is_empty())
        .map(|p| (p.token_b.clone(), p))
        .collect();

    info!(
        "Kalshi REST poller starting — {} tickers, public API (no auth required for prices)",
        tracked.len()
    );

    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert(
        reqwest::header::ACCEPT,
        "application/json".parse().unwrap(),
    );
    // Only add Authorization if a key is present (live trading convenience)
    if !api_key.is_empty() {
        if let Ok(val) = format!("Token {api_key}").parse() {
            headers.insert(reqwest::header::AUTHORIZATION, val);
        }
    }

    let client = reqwest::Client::builder()
        .default_headers(headers)
        .timeout(std::time::Duration::from_secs(5))
        .build()?;

    let poll_interval = tokio::time::Duration::from_secs(2);

    loop {
        for (ticker, pair) in &tracked {
            let url = format!("{api_url}/markets/{ticker}/orderbook");

            match client.get(&url).send().await {
                Ok(resp) if resp.status().is_success() => {
                    match resp.text().await {
                        Ok(text) => {
                            match handle_orderbook(&text, pair, &price_map) {
                                Ok(()) => price_notify.notify_waiters(),
                                Err(e) => warn!("Kalshi parse error for {ticker}: {e}"),
                            }
                        }
                        Err(e) => warn!("Kalshi body error for {ticker}: {e}"),
                    }
                }
                Ok(resp) => {
                    let status = resp.status();
                    if status.as_u16() == 401 {
                        error!(
                            "Kalshi 401 for {ticker} — check KALSHI_API_URL points to \
                             https://api.elections.kalshi.com/trade-api/v2"
                        );
                    } else {
                        warn!("Kalshi orderbook {ticker}: HTTP {status}");
                    }
                }
                Err(e) => {
                    warn!("Kalshi request error for {ticker}: {e}");
                }
            }
        }

        tokio::time::sleep(poll_interval).await;
    }
}

/// Parse a Kalshi orderbook REST response and update the price map.
///
/// Response shape:
/// ```json
/// { "orderbook": { "yes": [[price_cents, qty], ...], "no": [[price_cents, qty], ...] } }
/// ```
/// Best YES bid = highest price in the yes array.
fn handle_orderbook(
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
        None => return Ok(()), // no asks yet — skip
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
