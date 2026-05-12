import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import tracker
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
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.05), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.02):
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockPoly.return_value.get_order_status = AsyncMock(return_value="matched")
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)
        MockKalshi.return_value.get_order_status = AsyncMock(return_value="matched")

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
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.05), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.02):
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockPoly.return_value.get_order_status = AsyncMock(return_value="matched")
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)
        MockKalshi.return_value.get_order_status = AsyncMock(return_value="matched")

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
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.05), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.02):
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockPoly.return_value.get_order_status = AsyncMock(return_value="matched")
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
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.05), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.02):
        MockPoly.return_value.place_order = AsyncMock(side_effect=ExecutorError("poly fail"))
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)
        MockKalshi.return_value.get_order_status = AsyncMock(return_value="matched")
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
async def test_timeout_on_poly_fill_triggers_cancel(config, db, cross_platform_gap):
    """If Polymarket order doesn't fill within timeout, it gets canceled."""
    config["dry_run"] = False
    poly_result = {"order_id": "poly_slow", "status": "open", "platform": "polymarket",
                   "token_id": "no_token_hex", "amount_usdc": 5.0}
    kalshi_result = {"order_id": "kal_ok", "status": "matched", "platform": "kalshi",
                     "ticker": "KXTEST-25DEC", "count": 11}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.1), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.05):
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockPoly.return_value.get_order_status = AsyncMock(return_value="open")  # never fills
        MockPoly.return_value.cancel_order = AsyncMock()
        MockPoly.return_value.close_order = AsyncMock()
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)
        MockKalshi.return_value.get_order_status = AsyncMock(return_value="matched")
        MockKalshi.return_value.close_order = AsyncMock()

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is None
    MockPoly.return_value.cancel_order.assert_called_once_with("poly_slow")


@pytest.mark.asyncio
async def test_both_legs_fill_returns_confirmation(config, db, cross_platform_gap):
    """Both legs fill → returns confirmation dict."""
    config["dry_run"] = False
    poly_result = {"order_id": "poly_ok", "status": "open", "platform": "polymarket",
                   "token_id": "no_token_hex", "amount_usdc": 5.0}
    kalshi_result = {"order_id": "kal_ok", "status": "resting", "platform": "kalshi",
                     "ticker": "KXTEST-25DEC", "count": 11}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.1), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.05):
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockPoly.return_value.get_order_status = AsyncMock(return_value="matched")
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)
        MockKalshi.return_value.get_order_status = AsyncMock(return_value="matched")

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is not None
    assert result["total_spent"] == 10.0


@pytest.mark.asyncio
async def test_dry_run_skips_fill_verification(db, cross_platform_gap):
    """Dry run must never call get_order_status."""
    dry_config = {
        "dry_run": True,
        "bankroll_usdc": 500.0, "kelly_fraction": 0.25,
        "min_bet_usdc": 10.0, "max_bet_usdc": 100.0,
        "polymarket_private_key": "", "polymarket_wallet_address": "",
        "kalshi_api_key": "", "kalshi_api_secret": "",
    }
    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi:
        ex = TwoLegExecutor(dry_config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is not None
    assert result["dry_run"] is True
    MockPoly.return_value.get_order_status.assert_not_called()
    MockKalshi.return_value.get_order_status.assert_not_called()


@pytest.mark.asyncio
async def test_urgent_gap_uses_aggressive_price(config, db):
    """Gap with closes_at < 30 min → policy returns urgency=high → price gets +0.03 buffer."""
    from datetime import datetime, timezone, timedelta

    urgent_gap = {
        "pair_type": "cross_platform",
        "market_id": "urgent-market",
        "polymarket_price": 0.70,
        "kalshi_price": 0.22,
        "gap_cents": 8.0,
        "confidence": "high",
        "polymarket_token": "no_tok",
        "kalshi_ticker": "KXURGENT",
        "kalshi_action": "buy",
        "fee_rate": 0.02,
        "closes_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }

    captured_price = []

    async def capture_place_order(token_id, side, amount_usdc, price=0.5, neg_risk=False, poly_liquidity_usdc=float("inf")):
        captured_price.append(price)
        return {"order_id": "poly_u", "status": "matched", "platform": "polymarket",
                "token_id": token_id, "amount_usdc": amount_usdc}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.05), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.02):
        MockPoly.return_value.place_order = capture_place_order
        MockPoly.return_value.get_order_status = AsyncMock(return_value="matched")
        MockKalshi.return_value.place_order = AsyncMock(return_value={
            "order_id": "kal_u", "status": "matched", "platform": "kalshi",
            "ticker": "KXURGENT", "count": 10,
        })
        MockKalshi.return_value.get_order_status = AsyncMock(return_value="matched")

        ex = TwoLegExecutor(config, db)
        await ex.execute(urgent_gap, bet_size=10.0)

    # Aggressive price = base_price + 0.03
    assert len(captured_price) > 0
    assert captured_price[0] > urgent_gap["polymarket_price"]


@pytest.mark.asyncio
async def test_fill_polls_run_concurrently(config, db, cross_platform_gap):
    """Both fill polls must run concurrently: total time ≈ 1×timeout, not 2×timeout."""
    import time

    TIMEOUT = 0.12  # 120ms timeout
    POLL = 0.02     # 20ms poll interval

    poly_result = {"order_id": "poly_slow", "status": "open", "amount_usdc": 5.0}
    kalshi_result = {"order_id": "kal_slow", "status": "resting", "ticker": "KXTEST-25DEC", "count": 5}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", TIMEOUT), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", POLL):
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result)
        MockPoly.return_value.get_order_status = AsyncMock(return_value="open")  # never fills
        MockPoly.return_value.cancel_order = AsyncMock()
        MockPoly.return_value.close_order = AsyncMock()
        MockPoly.return_value.get_balance = AsyncMock(return_value=1000.0)
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result)
        MockKalshi.return_value.get_order_status = AsyncMock(return_value="resting")  # never fills
        MockKalshi.return_value.cancel_order = AsyncMock()
        MockKalshi.return_value.close_order = AsyncMock()

        ex = TwoLegExecutor(config, db)
        t0 = time.monotonic()
        result = await ex.execute(cross_platform_gap, bet_size=10.0)
        elapsed = time.monotonic() - t0

    assert result is None
    # Sequential: ~2×TIMEOUT ≈ 0.24s. Concurrent: ~TIMEOUT ≈ 0.12s.
    assert elapsed < TIMEOUT * 2.0, (
        f"Fill polls appear sequential: {elapsed:.3f}s > {TIMEOUT * 2.0:.3f}s limit"
    )


@pytest.mark.asyncio
async def test_poly_partial_fill_routes_close_without_platform_in_result(config, db, cross_platform_gap):
    """close_order must fire even when place_order result lacks 'platform' field (real API behavior)."""
    # Real PolymarketExecutor.place_order() does NOT include 'platform' in its response
    poly_result_no_platform = {"order_id": "poly_real", "status": "matched",
                               "token_id": "no_token_hex", "amount_usdc": 5.0}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.05), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.02):
        MockPoly.return_value.place_order = AsyncMock(return_value=poly_result_no_platform)
        MockPoly.return_value.get_order_status = AsyncMock(return_value="matched")
        MockPoly.return_value.close_order = AsyncMock()
        MockKalshi.return_value.place_order = AsyncMock(side_effect=ExecutorError("kalshi fail"))

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is None
    # Emergency close MUST fire on the poly leg even without 'platform' key in result dict
    MockPoly.return_value.close_order.assert_called_once()
    row = db.execute("SELECT * FROM emergency_positions WHERE order_id='poly_real'").fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_kalshi_partial_fill_routes_close_without_platform_in_result(config, db, cross_platform_gap):
    """Kalshi close_order must fire even when place_order result lacks 'platform' field."""
    # Real KalshiExecutor.place_order() does NOT include 'platform' in its response
    kalshi_result_no_platform = {"order_id": "kal_real", "status": "resting",
                                 "ticker": "KXTEST-25DEC", "count": 11}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor") as MockKalshi, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.05), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.02):
        MockPoly.return_value.place_order = AsyncMock(side_effect=ExecutorError("poly fail"))
        MockKalshi.return_value.place_order = AsyncMock(return_value=kalshi_result_no_platform)
        MockKalshi.return_value.get_order_status = AsyncMock(return_value="matched")
        MockKalshi.return_value.close_order = AsyncMock()

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(cross_platform_gap, bet_size=10.0)

    assert result is None
    # Emergency close MUST fire on the kalshi leg even without 'platform' key in result dict
    MockKalshi.return_value.close_order.assert_called_once()
    row = db.execute("SELECT * FROM emergency_positions WHERE order_id='kal_real'").fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_fill_poll_records_fill_metric_on_success(config, db, cross_platform_gap):
    """fill_polls counter must be incremented when poll returns filled."""
    from prometheus_client import REGISTRY
    config["dry_run"] = False

    before = REGISTRY.get_sample_value(
        "arb_fill_polls_total",
        {"platform": "polymarket", "result": "filled"}
    ) or 0.0

    poly_result = {"order_id": "p1", "status": "matched", "token_id": "tok"}
    kal_result = {"order_id": "k1", "status": "matched", "ticker": "TICK"}

    with patch("two_leg_executor.PolymarketExecutor") as MP, \
         patch("two_leg_executor.KalshiExecutor") as MK, \
         patch("two_leg_executor._FILL_TIMEOUT", 0.1), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.05):
        MP.return_value.place_order = AsyncMock(return_value=poly_result)
        MP.return_value.get_order_status = AsyncMock(return_value="matched")
        MK.return_value.place_order = AsyncMock(return_value=kal_result)
        MK.return_value.get_order_status = AsyncMock(return_value="matched")

        ex = TwoLegExecutor(config, db)
        await ex.execute(cross_platform_gap, bet_size=10.0)

    after = REGISTRY.get_sample_value(
        "arb_fill_polls_total",
        {"platform": "polymarket", "result": "filled"}
    )
    assert after == before + 1.0


@pytest.mark.asyncio
async def test_internal_pair_uses_polymarket_for_both_legs(config, db, internal_gap):
    poly_result_a = {"order_id": "poly_a", "status": "matched", "platform": "polymarket",
                     "token_id": "tokenA_hex", "amount_usdc": 5.0}
    poly_result_b = {"order_id": "poly_b", "status": "matched", "platform": "polymarket",
                     "token_id": "tokenB_hex", "amount_usdc": 4.5}

    with patch("two_leg_executor.PolymarketExecutor") as MockPoly, \
         patch("two_leg_executor.KalshiExecutor"), \
         patch("two_leg_executor._FILL_TIMEOUT", 0.05), \
         patch("two_leg_executor._FILL_POLL_INTERVAL", 0.02):
        MockPoly.return_value.place_order = AsyncMock(
            side_effect=[poly_result_a, poly_result_b]
        )
        MockPoly.return_value.get_order_status = AsyncMock(return_value="matched")

        ex = TwoLegExecutor(config, db)
        result = await ex.execute(internal_gap, bet_size=10.0)

    assert result is not None
    assert MockPoly.return_value.place_order.call_count == 2


@pytest.mark.asyncio
async def test_internal_legb_fill_verified():
    """For internal pairs, leg_b (also Polymarket) must be fill-polled."""
    config = {"dry_run": False, "min_bet_usdc": 10.0, "max_bet_usdc": 100.0,
              "bankroll_usdc": 500.0, "kelly_fraction": 0.25}
    db = sqlite3.connect(":memory:")
    tracker.init_db(":memory:")

    poly_fills = []

    class MockPoly:
        async def place_order(self, token_id, side, amount_usdc, price, neg_risk):
            return {"order_id": f"ord-{token_id[:4]}", "status": "open"}
        async def get_order_status(self, order_id):
            poly_fills.append(order_id)
            return "matched"
        async def get_balance(self):
            return 999.0

    class MockKalshi:
        pass  # not used for internal pairs

    gap = {
        "pair_type": "internal",
        "market_id": "evt::tok1-tok2",
        "polymarket_token": "tok1",
        "kalshi_ticker": "tok2",  # token_b stored here for internal
        "polymarket_price": 0.45,
        "kalshi_price": 0.45,
        "gap_cents": 10.0,
        "confidence": "high",
        "poly_liquidity_usdc": 200.0,
        "kalshi_liquidity_usdc": 200.0,
        "fee_rate": 0.02,
    }
    executor = TwoLegExecutor.__new__(TwoLegExecutor)
    executor._config = config
    executor._db = db
    executor._poly = MockPoly()
    executor._kalshi = MockKalshi()

    await executor._execute_internal(gap, bet_size=20.0, price_buffer=0.0)

    # Both tok1 and tok2 order IDs should have been polled
    assert len(poly_fills) == 2, f"Expected 2 fill polls (both legs), got {len(poly_fills)}: {poly_fills}"
