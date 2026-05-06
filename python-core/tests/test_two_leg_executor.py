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
        # Direction 1: polymarket_price = NO price (0.70), kalshi_price = YES price (0.22)
        # combined = 0.70 + 0.22 = 0.92 → 8c gap
        "polymarket_price": 0.70,
        "kalshi_price": 0.22,
        "gap_cents": 8.0,
        "confidence": "medium",
        "polymarket_token": "no_token_hex",  # NO token ID
        "kalshi_ticker": "KXTEST-25DEC",
        "kalshi_action": "buy",
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
                   "token_id": "no_token_hex", "amount_usdc": 5.0}
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
async def test_direction2_gap_sells_kalshi(config, db):
    """Direction 2 gap (Poly YES + Kalshi NO) must call Kalshi with action='sell'."""
    rev_gap = {
        "pair_type": "cross_platform",
        "market_id": "test-market-rev",
        "polymarket_price": 0.28,   # YES price
        "kalshi_price": 0.65,       # NO price
        "gap_cents": 7.0,
        "confidence": "medium",
        "polymarket_token": "yes_token_hex",
        "kalshi_ticker": "KXTEST-25DEC",
        "kalshi_action": "sell",
        "fee_rate": 0.02,
    }
    poly_result = {"order_id": "poly_2", "status": "matched", "platform": "polymarket",
                   "token_id": "yes_token_hex", "amount_usdc": 2.0}
    kalshi_result = {"order_id": "kal_2", "status": "resting", "platform": "kalshi",
                     "ticker": "KXTEST-25DEC", "count": 10}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(rev_gap, bet_size=10.0)

    assert result is not None
    # Kalshi must be called with action="sell"
    call_kwargs = MockKalshi.return_value.place_order.call_args[1]
    assert call_kwargs.get("action") == "sell"


@pytest.mark.asyncio
async def test_polymarket_fills_kalshi_fails_emergency_close(config, db, cross_platform_gap):
    poly_result = {"order_id": "poly_1", "status": "matched", "platform": "polymarket",
                   "token_id": "no_token_hex", "amount_usdc": 5.0}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockPoly.return_value.close_order = AsyncMock()
        MockPoly.return_value.get_balance = AsyncMock(return_value=1000.0)
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
async def test_dry_run_returns_confirmation_without_api_calls(db, cross_platform_gap):
    dry_config = {
        "dry_run": True,
        "bankroll_usdc": 500.0,
        "kelly_fraction": 0.25,
        "min_bet_usdc": 10.0,
        "max_bet_usdc": 100.0,
        "polymarket_private_key": "",
        "polymarket_wallet_address": "",
        "kalshi_api_key": "",
        "kalshi_api_secret": "",
    }
    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        ex = TwoLegExecutor(dry_config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is not None
    assert result["dry_run"] is True
    assert result["polymarket_order_id"].startswith("dry-poly-")
    assert result["kalshi_order_id"].startswith("dry-kalshi-")
    assert result["total_spent"] == 10.0
    assert result["gap_cents"] == 8.0
    # Real executors must never be called in dry-run mode
    MockPoly.return_value.place_order.assert_not_called()
    MockKalshi.return_value.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_expected_profit_is_positive(db, cross_platform_gap):
    dry_config = {
        "dry_run": True,
        "bankroll_usdc": 500.0,
        "kelly_fraction": 0.25,
        "min_bet_usdc": 10.0,
        "max_bet_usdc": 100.0,
        "polymarket_private_key": "",
        "polymarket_wallet_address": "",
        "kalshi_api_key": "",
        "kalshi_api_secret": "",
    }
    with patch("two_leg_executor.PolymarketExecutor"), \
         patch("two_leg_executor.KalshiExecutor"):
        ex = TwoLegExecutor(dry_config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result["expected_profit"] > 0


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
