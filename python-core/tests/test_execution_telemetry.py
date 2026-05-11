import sys
import sqlite3
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock
sys.path.insert(0, str(Path(__file__).parent.parent))

from tracker import _create_tables, log_execution_event, update_execution_fill
from two_leg_executor import TwoLegExecutor


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_tables(conn)
    return conn


def test_log_execution_event_creates_row_with_signal_prices():
    db = make_db()
    now = datetime.now(timezone.utc).isoformat()
    evt = {
        "market_id": "test-mkt",
        "pair_type": "cross_platform",
        "signal_poly_price": 0.72,
        "signal_kalshi_price": 0.59,
        "signal_gap_cents": 13.0,
        "signal_poly_liquidity": 250.0,
        "signal_kalshi_liquidity": 180.0,
        "signal_at": now,
        "submitted_poly_price": 0.75,
        "submitted_kalshi_price": 0.59,
        "price_buffer_applied": 0.03,
        "submitted_at": now,
        "urgency": "high",
    }
    exec_id = log_execution_event(db, evt)
    assert exec_id is not None and exec_id > 0

    row = db.execute("SELECT * FROM execution_events WHERE id=?", (exec_id,)).fetchone()
    assert row["market_id"] == "test-mkt"
    assert abs(row["signal_poly_price"] - 0.72) < 1e-6
    assert abs(row["signal_kalshi_price"] - 0.59) < 1e-6
    assert abs(row["signal_gap_cents"] - 13.0) < 1e-6
    assert abs(row["signal_poly_liquidity"] - 250.0) < 1e-6
    assert abs(row["submitted_poly_price"] - 0.75) < 1e-6
    assert abs(row["price_buffer_applied"] - 0.03) < 1e-6
    assert row["urgency"] == "high"
    # fill fields not set yet
    assert row["filled_poly_price"] is None
    assert row["total_slippage_cents"] is None


def test_update_execution_fill_sets_slippage():
    db = make_db()
    now = datetime.now(timezone.utc).isoformat()
    exec_id = log_execution_event(db, {
        "market_id": "test-mkt",
        "pair_type": "cross_platform",
        "signal_poly_price": 0.70,
        "signal_kalshi_price": 0.60,
        "signal_gap_cents": 10.0,
        "signal_at": now,
    })
    update_execution_fill(db, exec_id, {
        "filled_poly_price": 0.73,
        "filled_kalshi_price": 0.61,
        "signal_poly_price": 0.70,
        "signal_kalshi_price": 0.60,
        "fill_latency_ms": 1500,
        "poly_fill_status": "matched",
        "kalshi_fill_status": "executed",
    })
    row = db.execute("SELECT * FROM execution_events WHERE id=?", (exec_id,)).fetchone()
    assert abs(row["filled_poly_price"] - 0.73) < 1e-6
    # poly slippage: (0.73 - 0.70) * 100 = 3.0 cents
    assert abs(row["poly_slippage_cents"] - 3.0) < 1e-4
    # kalshi slippage: (0.61 - 0.60) * 100 = 1.0 cent
    assert abs(row["kalshi_slippage_cents"] - 1.0) < 1e-4
    # total: 4.0 cents
    assert abs(row["total_slippage_cents"] - 4.0) < 1e-4
    assert row["fill_latency_ms"] == 1500
    assert row["poly_fill_status"] == "matched"


def test_dry_run_execute_does_not_create_execution_event():
    """Dry-run execution must not write to execution_events — no live data available."""
    db = make_db()
    config = {
        "dry_run": True,
        "min_bet_usdc": 10.0,
        "max_bet_usdc": 100.0,
        "bankroll_usdc": 500.0,
        "kelly_fraction": 0.25,
        "kalshi_api_key": "",
        "kalshi_api_secret": "",
        "kalshi_api_url": "https://api.elections.kalshi.com/trade-api/v2",
        "polymarket_private_key": "",
        "polymarket_wallet_address": "",
        "polymarket_signature_type": 0,
    }
    executor = TwoLegExecutor(config, db)
    gap = {
        "market_id": "test-mkt",
        "pair_type": "cross_platform",
        "polymarket_price": 0.72,
        "kalshi_price": 0.59,
        "gap_cents": 13.0,
        "confidence": "high",
        "polymarket_token": "tok123",
        "kalshi_ticker": "KX-TEST",
        "poly_liquidity_usdc": 250.0,
        "kalshi_liquidity_usdc": 180.0,
    }
    result = asyncio.run(executor.execute(gap))
    assert result is not None  # dry run always returns a confirmation
    count = db.execute("SELECT COUNT(*) FROM execution_events").fetchone()[0]
    assert count == 0, f"Dry run must not write execution_events, found {count} rows"
