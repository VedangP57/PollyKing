use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use anyhow::Result;
use log::debug;
use tokio::time::interval;

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
    // every 10ms. With Mutex every read was exclusive even though it was read-only.
    price_map: Arc<RwLock<HashMap<String, Price>>>,
    gap_tx: crossbeam_channel::Sender<Gap>,
) -> Result<()> {
    // Pre-compute keys once — no format!() in the hot loop.
    let keys = precompute_keys(&pairs);

    let mut ticker = interval(Duration::from_millis(10));

    loop {
        ticker.tick().await;

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

    // Direction 1: Poly NO + Kalshi YES
    let combined = poly.no_price + kalshi.yes_price;
    let gap_cents = (1.0 - combined) * 100.0;
    if gap_cents >= config.min_gap_cents && gap_cents <= config.max_gap_cents {
        debug!(
            "CrossPlatform gap: {} | PolyNO:{:.2} KalshiYES:{:.2} | {:.1}c",
            pair.market_id, poly.no_price, kalshi.yes_price, gap_cents
        );
        let gap = Gap::new(
            "cross_platform".into(),
            pair.market_id.clone(),
            poly.yes_price,
            kalshi.yes_price,
            pair.token_a.clone(),
            pair.token_b.clone(),
            gap_cents,
        );
        let _ = gap_tx.try_send(gap);
    }

    // Direction 2: Poly YES + Kalshi NO
    let combined_rev = poly.yes_price + kalshi.no_price;
    let gap_cents_rev = (1.0 - combined_rev) * 100.0;
    if gap_cents_rev >= config.min_gap_cents && gap_cents_rev <= config.max_gap_cents {
        debug!(
            "CrossPlatform gap (rev): {} | PolyYES:{:.2} KalshiNO:{:.2} | {:.1}c",
            pair.market_id, poly.yes_price, kalshi.no_price, gap_cents_rev
        );
        let mut gap = Gap::new(
            "cross_platform".into(),
            format!("{}-rev", pair.market_id),
            poly.yes_price,
            kalshi.yes_price,
            pair.token_a.clone(),
            pair.token_b.clone(),
            gap_cents_rev,
        );
        gap.gap_cents = gap_cents_rev;
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
            gap_cents,
        );
        let _ = gap_tx.try_send(gap);
    }
}

pub fn compute_gap_cents(price_a: f64, price_b: f64) -> f64 {
    (1.0 - (price_a + price_b)) * 100.0
}
