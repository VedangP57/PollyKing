import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from tracker import _create_tables, log_trade, log_gap, get_open_live_trades, resolve_trade
from reconciler import compute_actual_profit, ResolutionResult


def make_trade(db, *, amount_usdc=50.0, expected_profit=2.0, dry_run=False):
    gap_id = log_gap(db, {
        "market_id": "test::market",
        "polymarket_price": 0.45,
        "kalshi_price": 0.47,
        "gap_cents": 8.0,
        "confidence": "high",
    })
    return log_trade(db, {
        "gap_id": gap_id,
        "polymarket_order_id": "poly-123",
        "kalshi_order_id": "kal-456",
        "polymarket_side": "NO",
        "kalshi_side": "YES",
        "amount_usdc": amount_usdc,
        "expected_profit": expected_profit,
        "status": "open",
        "dry_run": dry_run,
    })


def test_compute_actual_profit_yes_resolution():
    result = compute_actual_profit(
        polymarket_side="NO",
        kalshi_side="YES",
        resolution="YES",
        amount_usdc=50.0,
        polymarket_amount=23.5,
        kalshi_amount=26.5,
    )
    assert isinstance(result, ResolutionResult)
    assert result.status in ("profit", "loss", "resolved")
    assert isinstance(result.actual_profit, float)


def test_compute_actual_profit_no_resolution():
    result = compute_actual_profit(
        polymarket_side="NO",
        kalshi_side="YES",
        resolution="NO",
        amount_usdc=50.0,
        polymarket_amount=23.5,
        kalshi_amount=26.5,
    )
    assert result.status in ("profit", "loss", "resolved")


def test_open_live_trades_excludes_dry(db):
    make_trade(db, dry_run=True)
    make_trade(db, dry_run=False)
    live = get_open_live_trades(db)
    assert len(live) == 1
    assert live[0]["polymarket_order_id"] == "poly-123"


def test_resolve_trade_updates_status(db):
    trade_id = make_trade(db, dry_run=False)
    resolve_trade(db, trade_id, actual_profit=1.5, status="profit")
    row = db.execute("SELECT actual_profit, status FROM trades WHERE id=?", (trade_id,)).fetchone()
    assert row["actual_profit"] == pytest.approx(1.5)
    assert row["status"] == "profit"
