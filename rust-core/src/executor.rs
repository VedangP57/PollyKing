use anyhow::Result;
use log::info;
use uuid::Uuid;

use crate::types::{AppConfig, ExecuteCommand, OrderPlaced};

pub async fn execute(cmd: ExecuteCommand, config: &AppConfig) -> Result<OrderPlaced> {
    if cmd.dry_run || config.dry_run {
        return dry_run_execute(cmd);
    }

    live_execute(cmd, config).await
}

fn dry_run_execute(cmd: ExecuteCommand) -> Result<OrderPlaced> {
    let poly_order_id = format!("dry_{}", &Uuid::new_v4().to_string()[..8]);
    let kalshi_order_id = format!("dry_{}", &Uuid::new_v4().to_string()[..8]);

    let total_spent = cmd.polymarket_amount + cmd.kalshi_amount;
    // combined = 1 - gap_cents/100; K contracts bought = total_spent / combined
    // payout = K (one leg always wins); gross = K - total_spent; net = gross - fee
    let combined = 1.0 - cmd.gap_cents / 100.0;
    let k = if combined > 0.0 { total_spent / combined } else { 0.0 };
    let fee = cmd.taker_fee_rate * total_spent;
    let expected_profit = k - total_spent - fee;

    info!(
        "DRY RUN | Poly {} ${:.2} | Kalshi {} ${:.2} | Gap {:.1}¢ | Fee ${:.2} | Net: +${:.2}",
        cmd.polymarket_side, cmd.polymarket_amount,
        cmd.kalshi_side, cmd.kalshi_amount,
        cmd.gap_cents, fee, expected_profit
    );

    Ok(OrderPlaced {
        event: "order_placed".to_string(),
        polymarket_order_id: poly_order_id,
        kalshi_order_id,
        total_spent,
        expected_profit,
        dry_run: true,
    })
}

async fn live_execute(cmd: ExecuteCommand, config: &AppConfig) -> Result<OrderPlaced> {
    let client = reqwest::Client::new();

    let (poly_result, kalshi_result) = tokio::join!(
        place_polymarket_order(&client, &cmd, config),
        place_kalshi_order(&client, &cmd, config),
    );

    let poly_order_id = poly_result?;
    let kalshi_order_id = kalshi_result?;

    let total_spent = cmd.polymarket_amount + cmd.kalshi_amount;
    let combined = 1.0 - cmd.gap_cents / 100.0;
    let k = if combined > 0.0 { total_spent / combined } else { 0.0 };
    let fee = cmd.taker_fee_rate * total_spent;
    let expected_profit = k - total_spent - fee;

    Ok(OrderPlaced {
        event: "order_placed".to_string(),
        polymarket_order_id: poly_order_id,
        kalshi_order_id,
        total_spent,
        expected_profit,
        dry_run: false,
    })
}

async fn place_polymarket_order(
    client: &reqwest::Client,
    cmd: &ExecuteCommand,
    config: &AppConfig,
) -> Result<String> {
    let body = serde_json::json!({
        "side": cmd.polymarket_side,
        "amount": cmd.polymarket_amount,
        "type": "market",
    });

    let resp = client
        .post(format!("{}/order", config.polymarket_clob_url))
        .header("Authorization", format!("Bearer {}", config.polymarket_api_key))
        .json(&body)
        .send()
        .await?;

    let json: serde_json::Value = resp.json().await?;
    let order_id = json["order"]["id"]
        .as_str()
        .unwrap_or("unknown")
        .to_string();

    Ok(order_id)
}

async fn place_kalshi_order(
    client: &reqwest::Client,
    cmd: &ExecuteCommand,
    config: &AppConfig,
) -> Result<String> {
    let body = serde_json::json!({
        "action": cmd.kalshi_side.to_lowercase(),
        "count": (cmd.kalshi_amount as u32),
        "type": "market",
    });

    let resp = client
        .post(format!("{}/portfolio/orders", config.kalshi_api_url))
        .header("Authorization", format!("Token {}", config.kalshi_api_key))
        .json(&body)
        .send()
        .await?;

    let json: serde_json::Value = resp.json().await?;
    let order_id = json["order"]["order_id"]
        .as_str()
        .unwrap_or("unknown")
        .to_string();

    Ok(order_id)
}
