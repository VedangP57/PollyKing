use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use anyhow::Result;
use chrono::Utc;
use log::{info, warn};
use serde_json::Value;

use crate::types::{Platform, Price};

// Number of Gamma market IDs to request per HTTP call.
// Gamma market IDs are short numeric strings ("540816"), so 100 per request
// keeps URLs well under 8KB and typically returns in < 300ms.
const BATCH_SIZE: usize = 100;

// Seconds between full poll cycles across all tracked markets.
const POLL_INTERVAL_SECS: u64 = 5;

// Polls Gamma REST API for live outcomePrices instead of using WebSocket.
// The ws-subscriptions.polymarket.com WebSocket domain no longer resolves (2026-05).
// Gamma /markets?id=X&id=Y&... returns outcomePrices[0] = YES price for each market.
//
// token_to_gamma: HashMap<token_id, gamma_market_id>
// Prices stored as poly:{token_id} in the shared price map.
pub async fn run(
    gamma_url: String,
    token_to_gamma: HashMap<String, String>,
    price_map: Arc<RwLock<HashMap<String, Price>>>,
) -> Result<()> {
    if token_to_gamma.is_empty() {
        info!("Polymarket: no tokens to track — REST poller idle.");
        return Ok(());
    }

    // Invert the map: gamma_id → token_id for price map writes
    let gamma_to_token: HashMap<String, String> = token_to_gamma
        .into_iter()
        .filter(|(tok, gid)| !tok.is_empty() && !gid.is_empty())
        .map(|(tok, gid)| (gid, tok))
        .collect();

    let gamma_ids: Vec<String> = gamma_to_token.keys().cloned().collect();
    let n_batches = (gamma_ids.len() + BATCH_SIZE - 1) / BATCH_SIZE;

    info!(
        "Polymarket REST poller: {} markets, {} batches per cycle, poll every {}s",
        gamma_ids.len(),
        n_batches,
        POLL_INTERVAL_SECS,
    );

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(10))
        .build()?;

    let mut cycle = 0u64;
    loop {
        let mut updates = 0usize;
        let mut errors = 0usize;

        for chunk in gamma_ids.chunks(BATCH_SIZE) {
            let query: String = chunk
                .iter()
                .map(|id| format!("id={id}"))
                .collect::<Vec<_>>()
                .join("&");
            let url = format!("{}/markets?{}", gamma_url.trim_end_matches('/'), query);

            match fetch_batch(&client, &url, &gamma_to_token, &price_map).await {
                Ok(n) => updates += n,
                Err(e) => {
                    warn!("Polymarket batch fetch error: {e}");
                    errors += 1;
                }
            }

            // Small inter-batch delay to avoid hammering the API
            tokio::time::sleep(Duration::from_millis(100)).await;
        }

        cycle += 1;
        if cycle % 10 == 1 {
            info!(
                "Polymarket poll cycle #{cycle}: {updates} prices updated, {errors} errors"
            );
        }

        tokio::time::sleep(Duration::from_secs(POLL_INTERVAL_SECS)).await;
    }
}

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
        if gamma_id.is_empty() {
            continue;
        }

        let token_id = match gamma_to_token.get(gamma_id) {
            Some(t) => t,
            None => continue,
        };

        // outcomePrices is a JSON-encoded string: '["0.545", "0.455"]'
        // Index 0 = YES price
        let yes_price: f64 = market["outcomePrices"]
            .as_str()
            .and_then(|s| serde_json::from_str::<Vec<String>>(s).ok())
            .and_then(|v| v.first()?.parse().ok())
            .unwrap_or(0.0);

        if yes_price == 0.0 {
            continue;
        }

        let price = Price {
            market_id: token_id.clone(),
            platform: Platform::Polymarket,
            yes_price,
            no_price: 1.0 - yes_price,
            timestamp: Utc::now(),
        };

        map.insert(format!("poly:{token_id}"), price);
        count += 1;
    }

    Ok(count)
}
