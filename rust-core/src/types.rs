use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Price {
    pub market_id: String,
    pub platform: Platform,
    pub yes_price: f64,   // best bid for YES (receive when selling YES)
    pub yes_ask: f64,     // best ask for YES (pay when buying YES)
    pub no_price: f64,    // 1.0 - yes_price (derived)
    pub bid_size: f64,    // top-of-book bid qty (contracts on Kalshi, shares on Polymarket)
    pub ask_size: f64,    // top-of-book ask qty
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
// Cross-platform: token_a = Polymarket YES hex ID, no_token_a = Polymarket NO hex ID, token_b = Kalshi ticker
// Internal:       token_a = Polymarket YES hex ID, no_token_a = "" (unused), token_b = second YES hex ID
#[derive(Debug, Clone)]
pub struct MarketPair {
    pub pair_type: PairType,
    pub token_a: String,       // Polymarket YES token
    pub no_token_a: String,    // Polymarket NO token (cross-platform only)
    pub token_b: String,
    pub market_id: String,
    pub gamma_id_a: String,
    pub gamma_id_b: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Gap {
    pub event: String,
    pub pair_type: String,
    pub market_id: String,
    /// Price of the Polymarket token being purchased (NO price for dir1, YES price for dir2/internal)
    pub polymarket_price: f64,
    /// Price of the Kalshi side being purchased (YES price for dir1, NO price for dir2)
    pub kalshi_price: f64,
    pub gap_cents: f64,
    /// Token ID to BUY on Polymarket (NO token for cross-platform dir1, YES token for dir2/internal)
    pub polymarket_token: String,
    pub kalshi_ticker: String,
    /// "buy" for Kalshi YES (dir1/internal), "sell" for Kalshi NO (dir2)
    pub kalshi_action: String,
    pub timestamp: String,
    /// Executable notional at top of Polymarket order book (ask_size × price)
    pub poly_liquidity_usdc: f64,
    /// Executable notional at top of Kalshi order book (ask_size × price)
    pub kalshi_liquidity_usdc: f64,
    /// Kalshi best-ask minus best-bid in cents; 0.0 for internal pairs (unknown)
    pub kalshi_spread_cents: f64,
}

impl Gap {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        pair_type: String,
        market_id: String,
        polymarket_price: f64,
        kalshi_price: f64,
        polymarket_token: String,
        kalshi_ticker: String,
        kalshi_action: String,
        gap_cents: f64,
        poly_liquidity_usdc: f64,
        kalshi_liquidity_usdc: f64,
    ) -> Self {
        Gap {
            event: "gap_detected".to_string(),
            pair_type,
            market_id,
            polymarket_price,
            kalshi_price,
            gap_cents,
            polymarket_token,
            kalshi_ticker,
            kalshi_action,
            timestamp: Utc::now().to_rfc3339(),
            poly_liquidity_usdc,
            kalshi_liquidity_usdc,
            kalshi_spread_cents: 0.0,
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
    #[serde(default)]
    pub taker_fee_rate: f64,
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
                .unwrap_or("wss://ws-subscriptions-clob.polymarket.com".into()),
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
