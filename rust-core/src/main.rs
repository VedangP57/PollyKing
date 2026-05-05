use arb::{bridge, comparator, executor, fetcher, types};

use std::collections::HashMap;
use std::sync::{Arc, Mutex, RwLock};

use anyhow::Result;
use log::info;

use types::{AppConfig, MarketPair};

#[tokio::main]
async fn main() -> Result<()> {
    dotenv::dotenv().ok();
    env_logger::init();

    let config = AppConfig::from_env()?;
    info!("Arb bot starting. DRY_RUN={}", config.dry_run);

    // Load market pairs from config/markets.json (passed via env or hardcoded path)
    let pairs = load_market_pairs();
    info!("Loaded {} market pairs", pairs.len());

    // RwLock: comparator reads every 10ms (read lock, zero clone, non-exclusive).
    // Fetchers write infrequently (every 2–5s) via write lock.
    let price_map: Arc<RwLock<HashMap<String, types::Price>>> =
        Arc::new(RwLock::new(HashMap::new()));

    let (gap_tx, gap_rx) = crossbeam_channel::bounded::<types::Gap>(1000);
    let (order_tx, order_rx) =
        crossbeam_channel::bounded::<(types::ExecuteCommand, types::Gap)>(100);

    let pending_gaps: Arc<Mutex<HashMap<String, types::Gap>>> =
        Arc::new(Mutex::new(HashMap::new()));

    // Build token → gamma_id map for REST price polling.
    // token_a always has a gamma_id; token_b has one only for internal pairs.
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

    // Cross-platform pairs only for Kalshi fetcher
    let kalshi_pairs: Vec<MarketPair> = pairs
        .iter()
        .filter(|p| p.pair_type == types::PairType::CrossPlatform)
        .cloned()
        .collect();

    // Spawn Polymarket REST poller
    let poly_map = Arc::clone(&price_map);
    let gamma_url = config.polymarket_gamma_url.clone();
    tokio::spawn(async move {
        if let Err(e) = fetcher::polymarket::run(gamma_url, token_to_gamma_id, poly_map).await {
            log::error!("Polymarket fetcher error: {e}");
        }
    });

    // Spawn Kalshi REST poller (public API — no key needed for price reads)
    let kalshi_map = Arc::clone(&price_map);
    let kalshi_api_url = config.kalshi_api_url.clone();  // https://api.elections.kalshi.com/...
    let kalshi_key = config.kalshi_api_key.clone();       // only needed for live trading
    let kalshi_secret = config.kalshi_api_secret.clone();
    tokio::spawn(async move {
        if let Err(e) =
            fetcher::kalshi::run(kalshi_api_url, kalshi_key, kalshi_secret, kalshi_pairs, kalshi_map)
                .await
        {
            log::error!("Kalshi fetcher error: {e}");
        }
    });

    // Spawn comparator
    let comp_map = Arc::clone(&price_map);
    let comp_pairs = pairs.clone();
    let comp_config = config.clone();
    let gap_tx_clone = gap_tx.clone();
    tokio::spawn(async move {
        if let Err(e) = comparator::run(comp_config, comp_pairs, comp_map, gap_tx_clone).await {
            log::error!("Comparator error: {e}");
        }
    });

    // Spawn executor listener
    let exec_config = config.clone();
    tokio::spawn(async move {
        while let Ok((cmd, _gap)) = order_rx.recv() {
            match executor::execute(cmd, &exec_config).await {
                Ok(confirmation) => {
                    if let Err(e) = bridge::write_confirmation(&confirmation).await {
                        log::error!("Failed to write confirmation: {e}");
                    }
                }
                Err(e) => log::error!("Executor error: {e}"),
            }
        }
    });

    // Run bridge in main task (blocks on stdin/stdout)
    bridge::run(gap_rx, order_tx, pending_gaps).await?;

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

    // New format: "pairs" array with token_a/token_b/pair_type
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
                    token_b: token_b.to_string(),
                    market_id: market_id.to_string(),
                    gamma_id_a: p["gamma_id_a"].as_str().unwrap_or("").to_string(),
                    gamma_id_b: p["gamma_id_b"].as_str().unwrap_or("").to_string(),
                })
            })
            .collect();
    }

    // Legacy fallback: "manual_pairs" with polymarket_slug/kalshi_ticker
    val["manual_pairs"]
        .as_array()
        .cloned()
        .unwrap_or_default()
        .iter()
        .filter_map(|p| {
            let slug = p["polymarket_slug"].as_str()?;
            let ticker = p["kalshi_ticker"].as_str()?;
            Some(MarketPair {
                pair_type: types::PairType::CrossPlatform,
                token_a: slug.to_string(),
                token_b: ticker.to_string(),
                market_id: slug.to_string(),
                gamma_id_a: String::new(),
                gamma_id_b: String::new(),
            })
        })
        .collect()
}
