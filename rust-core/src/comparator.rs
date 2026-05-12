use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use anyhow::Result;
use log::debug;
use tokio::sync::watch;

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
    mut price_watch_rx: watch::Receiver<u64>,
    gap_tx: crossbeam_channel::Sender<Gap>,
) -> Result<()> {
    // Pre-compute keys once — no format!() in the hot loop.
    let keys = precompute_keys(&pairs);

    loop {
        // Block until a fetcher signals a price update.
        // watch::changed() never drops events — the latest value is always buffered.
        let _ = price_watch_rx.changed().await;

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
    // poly.no_price = 1 - poly.yes_price = NO ask (correct: buying NO crosses the NO book)
    // kalshi.yes_ask = execution price for buying Kalshi YES (taker crosses the YES ask)
    let combined1 = poly.no_price + kalshi.yes_ask;
    let gap1 = (1.0 - combined1) * 100.0;
    if gap1 >= config.min_gap_cents && gap1 <= config.max_gap_cents {
        // Liquidity: poly NO side uses bid_size (selling YES = buying NO at the NO ask)
        // kalshi YES ask side uses ask_size
        let poly_liq1 = poly.bid_size * poly.no_price;
        let kalshi_liq1 = kalshi.ask_size * kalshi.yes_ask;
        if poly_liq1 < config.min_bet_usdc || kalshi_liq1 < config.min_bet_usdc {
            debug!(
                "CrossPlatform dir1 thin: {} poly_liq={:.1} kalshi_liq={:.1} < min={:.1}",
                pair.market_id, poly_liq1, kalshi_liq1, config.min_bet_usdc
            );
        } else if pair.no_token_a.is_empty() {
            debug!("CrossPlatform dir1 skipped — no_token_a missing for {}", pair.market_id);
        } else {
            debug!(
                "CrossPlatform dir1: {} | PolyNO:{:.4} KalshiYES(ask):{:.4} | {:.1}c",
                pair.market_id, poly.no_price, kalshi.yes_ask, gap1
            );
            let mut gap = Gap::new(
                "cross_platform".into(),
                pair.market_id.clone(),
                poly.no_price,    // execution price: buy Poly NO (crosses NO ask)
                kalshi.yes_ask,   // execution price: buy Kalshi YES (crosses YES ask)
                pair.no_token_a.clone(),
                pair.token_b.clone(),
                "buy".into(),
                gap1,
                poly_liq1,
                kalshi_liq1,
            );
            gap.kalshi_spread_cents = (kalshi.yes_ask - kalshi.yes_price) * 100.0;
            let _ = gap_tx.try_send(gap);
        }
    }

    // Direction 2: Buy Poly YES + Buy Kalshi NO (= sell Kalshi YES)
    // poly.yes_ask = execution price for buying Poly YES (taker crosses the YES ask)
    // kalshi.no_price = 1 - kalshi.yes_price = Kalshi NO ask (crosses NO book)
    let combined2 = poly.yes_ask + kalshi.no_price;
    let gap2 = (1.0 - combined2) * 100.0;
    if gap2 >= config.min_gap_cents && gap2 <= config.max_gap_cents {
        let poly_liq2 = poly.ask_size * poly.yes_ask;
        let kalshi_liq2 = kalshi.bid_size * kalshi.no_price;
        if poly_liq2 < config.min_bet_usdc || kalshi_liq2 < config.min_bet_usdc {
            debug!(
                "CrossPlatform dir2 thin: {} poly_liq={:.1} kalshi_liq={:.1} < min={:.1}",
                pair.market_id, poly_liq2, kalshi_liq2, config.min_bet_usdc
            );
        } else {
            debug!(
                "CrossPlatform dir2: {} | PolyYES(ask):{:.4} KalshiNO:{:.4} | {:.1}c",
                pair.market_id, poly.yes_ask, kalshi.no_price, gap2
            );
            let mut gap = Gap::new(
                "cross_platform".into(),
                format!("{}-rev", pair.market_id),
                poly.yes_ask,      // execution price: buy Poly YES (crosses YES ask)
                kalshi.no_price,   // execution price: buy Kalshi NO (crosses NO ask)
                pair.token_a.clone(),
                pair.token_b.clone(),
                "sell".into(),     // sell Kalshi YES = buy Kalshi NO
                gap2,
                poly_liq2,
                kalshi_liq2,
            );
            gap.kalshi_spread_cents = (kalshi.yes_ask - kalshi.yes_price) * 100.0;
            let _ = gap_tx.try_send(gap);
        }
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
        let poly_liq = price_a.ask_size * price_a.yes_price;
        let b_liq = price_b.ask_size * price_b.yes_price;
        if poly_liq < config.min_bet_usdc || b_liq < config.min_bet_usdc {
            debug!(
                "Internal thin: {} poly_liq={:.1} b_liq={:.1} < min={:.1}",
                pair.market_id, poly_liq, b_liq, config.min_bet_usdc
            );
            return;
        }
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
            poly_liq,
            b_liq,
        );
        let _ = gap_tx.try_send(gap);
    }
}

pub fn compute_gap_cents(price_a: f64, price_b: f64) -> f64 {
    (1.0 - (price_a + price_b)) * 100.0
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{Platform, Price};
    use chrono::Utc;

    fn make_config() -> AppConfig {
        AppConfig {
            dry_run: true,
            min_gap_cents: 5.0,
            max_gap_cents: 30.0,
            min_bet_usdc: 10.0,
            max_bet_usdc: 100.0,
            max_daily_loss_usdc: 50.0,
            max_open_positions: 5,
            polymarket_ws_url: String::new(),
            polymarket_clob_url: String::new(),
            polymarket_gamma_url: String::new(),
            kalshi_ws_url: String::new(),
            kalshi_api_url: String::new(),
            polymarket_api_key: String::new(),
            polymarket_private_key: String::new(),
            kalshi_api_key: String::new(),
            kalshi_api_secret: String::new(),
        }
    }

    fn make_price(platform: Platform, yes_bid: f64, yes_ask: f64) -> Price {
        Price {
            market_id: "test".into(),
            platform,
            yes_price: yes_bid,
            yes_ask,
            no_price: 1.0 - yes_bid,
            bid_size: 100.0,
            ask_size: 100.0,
            timestamp: Utc::now(),
        }
    }

    fn make_pair() -> (MarketPair, PairKeys) {
        let pair = MarketPair {
            pair_type: PairType::CrossPlatform,
            token_a: "tok-yes".into(),
            no_token_a: "tok-no".into(),
            token_b: "TICK-A".into(),
            market_id: "test-market".into(),
            gamma_id_a: String::new(),
            gamma_id_b: String::new(),
        };
        let keys = PairKeys {
            key_a: "poly:tok-yes".into(),
            key_b: "kalshi:TICK-A".into(),
        };
        (pair, keys)
    }

    #[test]
    fn test_dir1_gap_uses_kalshi_yes_ask_not_bid() {
        // Direction 1: Buy Poly NO + Buy Kalshi YES
        // kalshi bid=0.55, ask=0.60 — spread of 5¢
        // poly.no_price = 1 - 0.61 = 0.39
        // combined using bid: 0.39 + 0.55 = 0.94 → 6¢ (old code detects this — wrong)
        // combined using ask: 0.39 + 0.60 = 0.99 → 1¢ (below 5¢ threshold — no gap)
        let mut map = HashMap::new();
        map.insert("poly:tok-yes".into(), make_price(Platform::Polymarket, 0.61, 0.61));
        map.insert("kalshi:TICK-A".into(), make_price(Platform::Kalshi, 0.55, 0.60));

        let (pair, keys) = make_pair();
        let config = make_config();
        let (tx, rx) = crossbeam_channel::unbounded();

        check_cross_platform(&pair, &keys, &map, &config, &tx);

        assert!(
            rx.try_recv().is_err(),
            "dir1 must use kalshi.yes_ask (0.60), not yes_price bid (0.55) — bid gap is an illusion"
        );
    }

    #[test]
    fn test_dir2_gap_uses_poly_yes_ask_not_bid() {
        // Direction 2: Buy Poly YES + Buy Kalshi NO
        // poly bid=0.55, ask=0.60 — spread of 5¢
        // kalshi.no_price = 1 - 0.61 = 0.39
        // combined using bid: 0.55 + 0.39 = 0.94 → 6¢ (old code detects this — wrong)
        // combined using ask: 0.60 + 0.39 = 0.99 → 1¢ (below 5¢ threshold — no gap)
        let mut map = HashMap::new();
        map.insert("poly:tok-yes".into(), make_price(Platform::Polymarket, 0.55, 0.60));
        map.insert("kalshi:TICK-A".into(), make_price(Platform::Kalshi, 0.61, 0.61));

        let (pair, keys) = make_pair();
        let config = make_config();
        let (tx, rx) = crossbeam_channel::unbounded();

        check_cross_platform(&pair, &keys, &map, &config, &tx);

        assert!(
            rx.try_recv().is_err(),
            "dir2 must use poly.yes_ask (0.60), not yes_price bid (0.55) — bid gap is an illusion"
        );
    }

    #[test]
    fn test_thin_market_gap_not_emitted_when_below_threshold() {
        // Both sides have ask_size=0 → liquidity gate must suppress the gap
        // even though the price gap is valid (poly.no_price=0.39, kalshi.yes_ask=0.50 → gap=11¢)
        let mut map = HashMap::new();
        let mut poly = make_price(Platform::Polymarket, 0.61, 0.61);
        poly.ask_size = 0.0;   // no ask liquidity on poly
        let mut kalshi = make_price(Platform::Kalshi, 0.50, 0.50);
        kalshi.ask_size = 0.0; // no ask liquidity on kalshi
        map.insert("poly:tok-yes".into(), poly);
        map.insert("kalshi:TICK-A".into(), kalshi);

        let (pair, keys) = make_pair();
        let config = make_config(); // min_bet_usdc=10.0
        let (tx, rx) = crossbeam_channel::unbounded();

        check_cross_platform(&pair, &keys, &map, &config, &tx);

        assert!(
            rx.try_recv().is_err(),
            "thin market (ask_size=0) must not emit a gap"
        );
    }

    #[test]
    fn test_liquid_market_gap_emitted_with_liquidity_fields() {
        // poly: no_price=0.50, ask_size=200 → poly_liq=200*0.50=100
        // kalshi: yes_ask=0.40, ask_size=200 → kalshi_liq=200*0.40=80
        // gap = (1 - 0.50 - 0.40)*100 = 10¢ → above 5¢ threshold
        // Both liq > min_bet_usdc(10) → gap emitted
        let mut map = HashMap::new();
        let mut poly = make_price(Platform::Polymarket, 0.50, 0.50);
        poly.ask_size = 200.0;
        let mut kalshi = make_price(Platform::Kalshi, 0.55, 0.40);
        kalshi.ask_size = 200.0;
        map.insert("poly:tok-yes".into(), poly);
        map.insert("kalshi:TICK-A".into(), kalshi);

        let (pair, keys) = make_pair();
        let config = make_config();
        let (tx, rx) = crossbeam_channel::unbounded();

        check_cross_platform(&pair, &keys, &map, &config, &tx);

        let gap = rx.try_recv().expect("liquid market with valid gap must be emitted");
        assert!(gap.poly_liquidity_usdc >= 10.0, "poly_liquidity_usdc should be >= min_bet");
        assert!(gap.kalshi_liquidity_usdc >= 10.0, "kalshi_liquidity_usdc should be >= min_bet");
    }
}
