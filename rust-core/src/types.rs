use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Price {
    pub market_id: String,
    pub platform: Platform,
    pub yes_price: f64,
    pub no_price: f64,
    pub timestamp: DateTime<Utc>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum Platform {
    Polymarket,
    Kalshi,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum PairType {
    CrossPlatform,
    Internal,
}

// All pairs use token_a / token_b regardless of mode.
// Cross-platform: token_a = Polymarket YES hex ID, token_b = Kalshi ticker
// Internal:       token_a = Polymarket YES hex ID, token_b = second Polymarket YES hex ID
#[derive(Debug, Clone)]
pub struct MarketPair {
    pub pair_type: PairType,
    pub token_a: String,
    pub token_b: String,
    pub market_id: String,
    pub gamma_id_a: String,
    pub gamma_id_b: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Gap {
    pub event: String,
    pub pair_type: String,        // "cross_platform" or "internal"
    pub market_id: String,
    pub polymarket_price: f64,    // price of token_a (Polymarket YES)
    pub kalshi_price: f64,        // price of token_b (Kalshi YES or second Poly YES)
    pub gap_cents: f64,
    pub polymarket_token: String, // token_a
    pub kalshi_ticker: String,    // token_b
    pub timestamp: String,
}

impl Gap {
    pub fn new(
        pair_type: String,
        market_id: String,
        price_a: f64,
        price_b: f64,
        token_a: String,
        token_b: String,
        gap_cents: f64,
    ) -> Self {
        Gap {
            event: "gap_detected".to_string(),
            pair_type,
            market_id,
            polymarket_price: price_a,
            kalshi_price: price_b,
            gap_cents,
            polymarket_token: token_a,
            kalshi_ticker: token_b,
            timestamp: Utc::now().to_rfc3339(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExecuteCommand {
    pub action: String,
    #[serde(default = "default_cross_platform")]
    pub pair_type: String,
    pub polymarket_side: String,
    pub polymarket_amount: f64,
    pub kalshi_side: String,
    pub kalshi_amount: f64,
    #[serde(default)]
    pub gap_cents: f64,
    pub dry_run: bool,
}

fn default_cross_platform() -> String {
    "cross_platform".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderPlaced {
    pub event: String,
    pub polymarket_order_id: String,
    pub kalshi_order_id: String,
    pub total_spent: f64,
    pub expected_profit: f64,
    pub dry_run: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppConfig {
    pub dry_run: bool,
    pub min_gap_cents: f64,
    pub max_gap_cents: f64,
    pub min_bet_usdc: f64,
    pub max_bet_usdc: f64,
    pub max_daily_loss_usdc: f64,
    pub max_open_positions: u32,

    pub polymarket_ws_url: String,
    pub polymarket_clob_url: String,
    pub polymarket_gamma_url: String,
    pub kalshi_ws_url: String,
    pub kalshi_api_url: String,

    pub polymarket_api_key: String,
    pub polymarket_private_key: String,
    pub kalshi_api_key: String,
    pub kalshi_api_secret: String,
}

impl AppConfig {
    pub fn from_env() -> anyhow::Result<Self> {
        Ok(AppConfig {
            dry_run: std::env::var("DRY_RUN").unwrap_or("true".into()) == "true",
            min_gap_cents: std::env::var("MIN_GAP_CENTS").unwrap_or("5".into()).parse()?,
            max_gap_cents: std::env::var("MAX_GAP_CENTS").unwrap_or("30".into()).parse()?,
            min_bet_usdc: std::env::var("MIN_BET_USDC").unwrap_or("10".into()).parse()?,
            max_bet_usdc: std::env::var("MAX_BET_USDC").unwrap_or("100".into()).parse()?,
            max_daily_loss_usdc: std::env::var("MAX_DAILY_LOSS_USDC").unwrap_or("50".into()).parse()?,
            max_open_positions: std::env::var("MAX_OPEN_POSITIONS").unwrap_or("5".into()).parse()?,
            polymarket_ws_url: std::env::var("POLYMARKET_WS_URL")
                .unwrap_or("wss://ws-subscriptions.polymarket.com/ws/market".into()),
            polymarket_clob_url: std::env::var("POLYMARKET_CLOB_URL")
                .unwrap_or("https://clob.polymarket.com".into()),
            polymarket_gamma_url: std::env::var("POLYMARKET_GAMMA_URL")
                .unwrap_or("https://gamma-api.polymarket.com".into()),
            kalshi_ws_url: std::env::var("KALSHI_WS_URL")
                .unwrap_or("wss://trading-api.kalshi.com/trade-api/ws/v2".into()),
            kalshi_api_url: std::env::var("KALSHI_API_URL")
                .unwrap_or("https://api.elections.kalshi.com/trade-api/v2".into()),
            polymarket_api_key: std::env::var("POLYMARKET_API_KEY").unwrap_or_default(),
            polymarket_private_key: std::env::var("POLYMARKET_PRIVATE_KEY").unwrap_or_default(),
            kalshi_api_key: std::env::var("KALSHI_API_KEY").unwrap_or_default(),
            kalshi_api_secret: std::env::var("KALSHI_API_SECRET").unwrap_or_default(),
        })
    }
}
