import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from tracker import _create_tables, log_trade, log_gap, get_open_live_trades, resolve_trade
from reconciler import compute_actual_profit, ResolutionResult, _fetch_kalshi_settlement


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


def test_compute_profit_yes_resolution_cross_platform():
    # combined = 1.0 - 8.0/100 = 0.92
    # k = 10.0 / 0.92 = 10.8696...
    # gross = 10.8696 - 10.0 = 0.8696
    # fee = 0.02 * 10 = 0.20
    # net = 0.8696 - 0.20 = 0.6696 ≈ 0.67
    result = compute_actual_profit(
        poly_side="NO",
        kalshi_side="YES",
        resolution="YES",
        amount_usdc=10.0,
        gap_cents=8.0,
        fee_rate=0.02,
    )
    assert isinstance(result, ResolutionResult)
    assert result.status == "profit"
    assert abs(result.actual_profit - 0.67) < 0.02


def test_compute_profit_no_resolution_cross_platform():
    # Same formula — arb pays regardless of which side wins
    result = compute_actual_profit(
        poly_side="NO",
        kalshi_side="YES",
        resolution="NO",
        amount_usdc=10.0,
        gap_cents=8.0,
        fee_rate=0.02,
    )
    assert result.status == "profit"
    assert abs(result.actual_profit - 0.67) < 0.02


def test_compute_profit_internal_pair():
    # combined = 1.0 - 5.0/100 = 0.95
    # k = 10.0 / 0.95 = 10.526
    # gross = 0.526, fee = 0.04*10 = 0.40, net = 0.126
    result = compute_actual_profit(
        poly_side="YES",
        kalshi_side="YES",
        resolution="YES",
        amount_usdc=10.0,
        gap_cents=5.0,
        fee_rate=0.04,
    )
    assert result.status == "profit"
    assert result.actual_profit > 0


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


@pytest.mark.asyncio
async def test_fetch_kalshi_settlement_settled_yes():
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"market": {"status": "settled", "result": "yes"}})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    result = await _fetch_kalshi_settlement(mock_session, "KXTEST-25DEC")
    assert result == "YES"


@pytest.mark.asyncio
async def test_fetch_kalshi_settlement_settled_no():
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"market": {"status": "settled", "result": "no"}})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    result = await _fetch_kalshi_settlement(mock_session, "KXTEST-25DEC")
    assert result == "NO"


@pytest.mark.asyncio
async def test_fetch_kalshi_settlement_open_returns_none():
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"market": {"status": "open", "result": None}})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    result = await _fetch_kalshi_settlement(mock_session, "KXTEST-25DEC")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_kalshi_settlement_404_returns_none():
    mock_resp = AsyncMock()
    mock_resp.status = 404
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    result = await _fetch_kalshi_settlement(mock_session, "KXTEST-25DEC")
    assert result is None
