import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from tracker import _create_tables, log_trade, log_gap, resolve_trade
from calibration import compute_brier_score, compute_ev_error, compute_win_rate


def insert_resolved_trade(db, expected_profit, actual_profit, amount_usdc=50.0):
    gap_id = log_gap(db, {
        "market_id": "test::market",
        "polymarket_price": 0.45,
        "kalshi_price": 0.47,
        "gap_cents": 8.0,
        "confidence": "high",
    })
    trade_id = log_trade(db, {
        "gap_id": gap_id,
        "polymarket_order_id": "poly-x",
        "kalshi_order_id": "kal-y",
        "polymarket_side": "NO",
        "kalshi_side": "YES",
        "amount_usdc": amount_usdc,
        "expected_profit": expected_profit,
        "status": "open",
        "dry_run": False,
    })
    resolve_trade(db, trade_id, actual_profit, "profit" if actual_profit >= 0 else "loss")
    return trade_id


def test_brier_score_returns_float_when_resolved_trades_exist(db):
    for _ in range(5):
        insert_resolved_trade(db, expected_profit=2.0, actual_profit=1.8)
    score = compute_brier_score(db)
    assert score is not None
    assert 0.0 <= score <= 1.0


def test_brier_score_none_when_no_resolved_trades(db):
    score = compute_brier_score(db)
    assert score is None


def test_ev_error_positive(db):
    insert_resolved_trade(db, expected_profit=3.0, actual_profit=2.0)
    insert_resolved_trade(db, expected_profit=2.0, actual_profit=1.5)
    err = compute_ev_error(db)
    assert err is not None
    # Mean error = (3-2 + 2-1.5)/2 = 0.75
    assert err == pytest.approx(0.75, abs=1e-3)


def test_win_rate(db):
    insert_resolved_trade(db, expected_profit=2.0, actual_profit=1.5)   # profit
    insert_resolved_trade(db, expected_profit=2.0, actual_profit=-1.0)  # loss
    insert_resolved_trade(db, expected_profit=2.0, actual_profit=0.5)   # profit
    rate = compute_win_rate(db)
    assert rate == pytest.approx(2 / 3, abs=1e-3)
