import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tracker import _create_tables
from two_leg_executor import TwoLegExecutor
from kalshi_executor import ExecutorError


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_tables(conn)
    yield conn
    conn.close()


@pytest.fixture
def config():
    return {
        "dry_run": False,
        "bankroll_usdc": 500.0,
        "kelly_fraction": 0.25,
        "min_bet_usdc": 10.0,
        "max_bet_usdc": 100.0,
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
        "kalshi_api_key": "test_key",
        "kalshi_api_secret": "test_secret_32bytes_padding_here!",
        "kalshi_api_url": "https://api.elections.kalshi.com/trade-api/v2",
    }


@pytest.fixture
def cross_platform_gap():
    return {
        "pair_type": "cross_platform",
        "market_id": "test-market",
        "polymarket_price": 0.30,  # Poly YES=0.30, Poly NO=0.70
        "kalshi_price": 0.22,      # Kalshi YES=0.22
        # combined = 0.70 + 0.22 = 0.92 -> 8c gap
        "gap_cents": 8.0,
        "confidence": "medium",
        "polymarket_token": "abc123token",
        "kalshi_ticker": "KXTEST-25DEC",
        "fee_rate": 0.02,
    }


@pytest.fixture
def internal_gap():
    return {
        "pair_type": "internal",
        "market_id": "99::tokenA-tokenB",
        "polymarket_price": 0.50,
        "kalshi_price": 0.45,
        # combined = 0.50 + 0.45 = 0.95 -> 5c gap
        "gap_cents": 5.0,
        "confidence": "high",
        "polymarket_token": "tokenA_hex",
        "kalshi_ticker": "tokenB_hex",
        "fee_rate": 0.02,
    }


@pytest.mark.asyncio
async def test_both_legs_succeed_cross_platform(config, db, cross_platform_gap):
    poly_result = {"order_id": "poly_1", "status": "matched", "platform": "polymarket",
                   "token_id": "abc123token", "amount_usdc": 5.0}
    kalshi_result = {"order_id": "kal_1", "status": "resting", "platform": "kalshi",
                     "ticker": "KXTEST-25DEC", "count": 11}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is not None
    assert result["polymarket_order_id"] == "poly_1"
    assert result["kalshi_order_id"] == "kal_1"
    assert result["total_spent"] > 0
    assert result["gap_cents"] == 8.0


@pytest.mark.asyncio
async def test_both_legs_fail_returns_none(config, db, cross_platform_gap):
    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        MockPoly.return_value.place_order = AsyncMock(side_effect=ExecutorError("poly fail"))
        MockKalshi.return_value.place_order = AsyncMock(side_effect=ExecutorError("kalshi fail"))

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is None
    row = db.execute("SELECT COUNT(*) FROM emergency_positions").fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_polymarket_fills_kalshi_fails_emergency_close(config, db, cross_platform_gap):
    poly_result = {"order_id": "poly_1", "status": "matched", "platform": "polymarket",
                   "token_id": "abc123token", "amount_usdc": 5.0}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockPoly.return_value.close_order = AsyncMock()
        MockKalshi.return_value.place_order = AsyncMock(side_effect=ExecutorError("kalshi fail"))

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is None
    MockPoly.return_value.close_order.assert_called_once()
    row = db.execute("SELECT * FROM emergency_positions WHERE order_id='poly_1'").fetchone()
    assert row is not None
    assert row["status"] in ("open", "closed_auto")


@pytest.mark.asyncio
async def test_kalshi_fills_polymarket_fails_emergency_close(config, db, cross_platform_gap):
    kalshi_result = {"order_id": "kal_1", "status": "resting", "platform": "kalshi",
                     "ticker": "KXTEST-25DEC", "count": 11}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        MockPoly.return_value.place_order = AsyncMock(side_effect=ExecutorError("poly fail"))
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)
        MockKalshi.return_value.close_order = AsyncMock()

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is None
    MockKalshi.return_value.close_order.assert_called_once()
    row = db.execute("SELECT * FROM emergency_positions WHERE order_id='kal_1'").fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_internal_pair_uses_polymarket_for_both_legs(config, db, internal_gap):
    poly_result_a = {"order_id": "poly_a", "status": "matched", "platform": "polymarket",
                     "token_id": "tokenA_hex", "amount_usdc": 5.0}
    poly_result_b = {"order_id": "poly_b", "status": "matched", "platform": "polymarket",
                     "token_id": "tokenB_hex", "amount_usdc": 4.5}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor"):
        MockPoly.return_value.place_order = AsyncMock(
            side_effect=[poly_result_a, poly_result_b]
        )

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(internal_gap, bet_size=10.0)

    assert result is not None
    assert MockPoly.return_value.place_order.call_count == 2
