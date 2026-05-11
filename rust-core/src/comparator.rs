use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use anyhow::Result;
use log::debug;
use tokio::sync::Notify;

use crate::types::{AppConfig, Gap, MarketPair, PairType, Price};

/// Pre-computed key pair so the hot loop never calls `format!()`.
struct PairKeys {
    key_a: String,
    key_b: String,
}

/// Pre-compute all price-map lookup keys once at startup.
/// Hot loop just does `map.get(&keys.key_a)` — zero allocations.
fn precompute_keys(pairs: &[MarketPair]) -> Vec<PairKeys> {
    pairs
        .iter()
        .map(|p| match p.pair_type {
            PairType::CrossPlatform => PairKeys {
                key_a: format!("poly:{}", p.token_a),
                key_b: format!("kalshi:{}", p.token_b),
            },
            PairType::Internal => PairKeys {
                key_a: format!("poly:{}", p.token_a),
                key_b: format!("poly:{}", p.token_b),
            },
        })
        .collect()
}

pub async fn run(
    config: AppConfig,
    pairs: Vec<MarketPair>,
    // RwLock: many readers can hold simultaneously without blocking each other.
    // The fetchers write infrequently (once per poll cycle); the comparator reads
    // only when a fetcher has written a new price. With Mutex every read was
    // exclusive even though it was read-only.
    price_map: Arc<RwLock<HashMap<String, Price>>>,
    price_notify: Arc<Notify>,
    gap_tx: crossbeam_channel::Sender<Gap>,
) -> Result<()> {
    // Pre-compute keys once — no format!() in the hot loop.
    let keys = precompute_keys(&pairs);

    loop {
        price_notify.notified().await;

        // Read lock: does NOT clone the entire map.
        // Held only for the duration of the loop below (~microseconds).
        let map = price_map.read().unwrap();

        for (pair, pk) in pairs.iter().zip(keys.iter()) {
            match pair.pair_type {
                PairType::CrossPlatform => {
                    check_cross_platform(pair, pk, &map, &config, &gap_tx);
                }
                PairType::Internal => {
                    check_internal(pair, pk, &map, &config, &gap_tx);
                }
            }
        }
        // map (read guard) dropped here
    }
}

fn check_cross_platform(
    pair: &MarketPair,
    keys: &PairKeys,
    map: &HashMap<String, Price>,
    config: &AppConfig,
    gap_tx: &crossbeam_channel::Sender<Gap>,
) {
    let poly = match map.get(&keys.key_a) {
        Some(p) => p,
        None => return,
    };
    let kalshi = match map.get(&keys.key_b) {
        Some(p) => p,
        None => return,
    };

    // Direction 1: Buy Poly NO + Buy Kalshi YES
    // combined = poly.no_price + kalshi.yes_price
    let combined1 = poly.no_price + kalshi.yes_price;
    let gap1 = (1.0 - combined1) * 100.0;
    if gap1 >= config.min_gap_cents && gap1 <= config.max_gap_cents {
        if pair.no_token_a.is_empty() {
            // no_token_a not populated — skip to avoid buying wrong side
            debug!("CrossPlatform dir1 skipped — no_token_a missing for {}", pair.market_id);
        } else {
            debug!(
                "CrossPlatform dir1: {} | PolyNO:{:.4} KalshiYES:{:.4} | {:.1}c",
                pair.market_id, poly.no_price, kalshi.yes_price, gap1
            );
            let gap = Gap::new(
                "cross_platform".into(),
                pair.market_id.clone(),
                poly.no_price,      // price of the token being bought (NO)
                kalshi.yes_price,   // price of the Kalshi side (YES)
                pair.no_token_a.clone(),
                pair.token_b.clone(),
                "buy".into(),
                gap1,
            );
            let _ = gap_tx.try_send(gap);
        }
    }

    // Direction 2: Buy Poly YES + Buy Kalshi NO (= sell Kalshi YES)
    // combined = poly.yes_price + kalshi.no_price
    let combined2 = poly.yes_price + kalshi.no_price;
    let gap2 = (1.0 - combined2) * 100.0;
    if gap2 >= config.min_gap_cents && gap2 <= config.max_gap_cents {
        debug!(
            "CrossPlatform dir2: {} | PolyYES:{:.4} KalshiNO:{:.4} | {:.1}c",
            pair.market_id, poly.yes_price, kalshi.no_price, gap2
        );
        let gap = Gap::new(
            "cross_platform".into(),
            format!("{}-rev", pair.market_id),
            poly.yes_price,    // price of the token being bought (YES)
            kalshi.no_price,   // price of the Kalshi side (NO)
            pair.token_a.clone(),
            pair.token_b.clone(),
            "sell".into(),     // sell Kalshi YES = buy Kalshi NO
            gap2,
        );
        let _ = gap_tx.try_send(gap);
    }
}

fn check_internal(
    pair: &MarketPair,
    keys: &PairKeys,
    map: &HashMap<String, Price>,
    config: &AppConfig,
    gap_tx: &crossbeam_channel::Sender<Gap>,
) {
    let price_a = match map.get(&keys.key_a) {
        Some(p) => p,
        None => return,
    };
    let price_b = match map.get(&keys.key_b) {
        Some(p) => p,
        None => return,
    };

    let combined = price_a.yes_price + price_b.yes_price;
    let gap_cents = (1.0 - combined) * 100.0;

    if gap_cents >= config.min_gap_cents && gap_cents <= config.max_gap_cents {
        debug!(
            "Internal gap: {} | A:{:.2} B:{:.2} | {:.1}c",
            pair.market_id, price_a.yes_price, price_b.yes_price, gap_cents
        );
        let gap = Gap::new(
            "internal".into(),
            pair.market_id.clone(),
            price_a.yes_price,
            price_b.yes_price,
            pair.token_a.clone(),
            pair.token_b.clone(),
            "buy".into(),
            gap_cents,
        );
        let _ = gap_tx.try_send(gap);
    }
}

pub fn compute_gap_cents(price_a: f64, price_b: f64) -> f64 {
    (1.0 - (price_a + price_b)) * 100.0
}
