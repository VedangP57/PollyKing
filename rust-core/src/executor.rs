use anyhow::Result;
use log::info;
use uuid::Uuid;

use crate::types::{ExecuteCommand, OrderPlaced};

// live_execute removed — all order placement is handled by Python (py_clob_client + HMAC).
// dry_run_execute kept for testing and simulation.

pub fn dry_run_execute(cmd: ExecuteCommand) -> Result<OrderPlaced> {
    let poly_order_id = format!("dry_{}", &Uuid::new_v4().to_string()[..8]);
    let kalshi_order_id = format!("dry_{}", &Uuid::new_v4().to_string()[..8]);

    let total_spent = cmd.polymarket_amount + cmd.kalshi_amount;
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
