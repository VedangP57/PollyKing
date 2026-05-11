use arb::{bridge, comparator, fetcher, types};

use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use tokio::sync::Notify;

use anyhow::Result;
use log::info;

use types::{AppConfig, MarketPair};

#[tokio::main]
async fn main() -> Result<()> {
    dotenv::dotenv().ok();
    env_logger::init();

    let config = AppConfig::from_env()?;
    info!("Arb bot starting. DRY_RUN={}", config.dry_run);

    let pairs = load_market_pairs();
    info!("Loaded {} market pairs", pairs.len());

    let price_map: Arc<RwLock<HashMap<String, types::Price>>> =
        Arc::new(RwLock::new(HashMap::new()));

    // price_notify: fetchers call notify_waiters() after each price write.
    // comparator wakes up instead of spinning every 10ms.
    let price_notify: Arc<Notify> = Arc::new(Notify::new());

    let (gap_tx, gap_rx) = crossbeam_channel::bounded::<types::Gap>(1000);

    let token_to_gamma_id: HashMap<String, String> = pairs
        .iter()
        .flat_map(|p| {
            let mut entries = vec![(p.token_a.clone(), p.gamma_id_a.clone())];
            if p.pair_type == types::PairType::Internal {
                entries.push((p.token_b.clone(), p.gamma_id_b.clone()));
            }
            entries
        })
        .filter(|(tok, gid)| !tok.is_empty() && !gid.is_empty())
        .collect();

    let kalshi_pairs: Vec<MarketPair> = pairs
        .iter()
        .filter(|p| p.pair_type == types::PairType::CrossPlatform)
        .cloned()
        .collect();

    // Spawn Polymarket REST poller
    let poly_map = Arc::clone(&price_map);
    let poly_notify = Arc::clone(&price_notify);
    let gamma_url = config.polymarket_gamma_url.clone();
    tokio::spawn(async move {
        if let Err(e) = fetcher::polymarket::run(gamma_url, token_to_gamma_id, poly_map, poly_notify).await {
            log::error!("Polymarket fetcher error: {e}");
        }
    });

    // Spawn Kalshi REST poller
    let kalshi_map = Arc::clone(&price_map);
    let kalshi_notify = Arc::clone(&price_notify);
    let kalshi_api_url = config.kalshi_api_url.clone();
    let kalshi_key = config.kalshi_api_key.clone();
    let kalshi_secret = config.kalshi_api_secret.clone();
    tokio::spawn(async move {
        if let Err(e) =
            fetcher::kalshi::run(kalshi_api_url, kalshi_key, kalshi_secret, kalshi_pairs, kalshi_map, kalshi_notify)
                .await
        {
            log::error!("Kalshi fetcher error: {e}");
        }
    });

    // Spawn comparator
    let comp_map = Arc::clone(&price_map);
    let comp_notify = Arc::clone(&price_notify);
    let comp_pairs = pairs.clone();
    let comp_config = config.clone();
    tokio::spawn(async move {
        if let Err(e) = comparator::run(comp_config, comp_pairs, comp_map, comp_notify, gap_tx).await {
            log::error!("Comparator error: {e}");
        }
    });

    // Bridge: write gaps to stdout (one-directional — Python handles all execution)
    bridge::run(gap_rx).await?;

    Ok(())
}

fn load_market_pairs() -> Vec<MarketPair> {
    let path = std::env::var("MARKETS_JSON")
        .unwrap_or_else(|_| "config/markets.json".to_string());

    let data = match std::fs::read_to_string(&path) {
        Ok(d) => d,
        Err(_) => return vec![],
    };

    let val: serde_json::Value = match serde_json::from_str(&data) {
        Ok(v) => v,
        Err(_) => return vec![],
    };

    if let Some(pairs) = val["pairs"].as_array() {
        return pairs
            .iter()
            .filter_map(|p| {
                let token_a = p["token_a"].as_str()?;
                let token_b = p["token_b"].as_str()?;
                let market_id = p["market_id"].as_str().unwrap_or(token_a);
                let pair_type = match p["pair_type"].as_str().unwrap_or("cross_platform") {
                    "internal" => types::PairType::Internal,
                    _ => types::PairType::CrossPlatform,
                };
                Some(MarketPair {
                    pair_type,
                    token_a: token_a.to_string(),
                    no_token_a: p["no_token_a"].as_str().unwrap_or("").to_string(),
                    token_b: token_b.to_string(),
                    market_id: market_id.to_string(),
                    gamma_id_a: p["gamma_id_a"].as_str().unwrap_or("").to_string(),
                    gamma_id_b: p["gamma_id_b"].as_str().unwrap_or("").to_string(),
                })
            })
            .collect();
    }

    vec![]
}
