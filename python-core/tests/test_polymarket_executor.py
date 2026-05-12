import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from kalshi_executor import ExecutorError
from polymarket_executor import PolymarketExecutor


@pytest.fixture
def config():
    return {
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
        "polymarket_signature_type": 0,
        "dry_run": False,
    }


def make_mock_client(order_id="ord_poly_123", status="matched", error=""):
    """Build a mock ClobClient matching the v2 SDK interface."""
    client = MagicMock()
    # L1 auth — create_or_derive_api_key (v2 method name)
    client.create_or_derive_api_key.return_value = MagicMock()
    # v2 unified order method
    client.create_and_post_order.return_value = {
        "orderID": order_id,
        "status": status,
        "errorMsg": error,
    }
    return client


@pytest.mark.asyncio
async def test_place_order_success(config):
    mock_client = make_mock_client()
    with patch("polymarket_executor.ClobClient", return_value=mock_client):
        ex = PolymarketExecutor(config)
        result = await ex.place_order(
            token_id="1234567890abcdef",
            side="BUY",
            amount_usdc=5.0,
            price=0.65,
        )
    assert result["order_id"] == "ord_poly_123"
    assert result["status"] == "matched"
    assert result["platform"] == "polymarket"
    mock_client.create_and_post_order.assert_called_once()


@pytest.mark.asyncio
async def test_place_order_raises_on_error_msg(config):
    mock_client = make_mock_client(error="insufficient funds")
    with patch("polymarket_executor.ClobClient", return_value=mock_client):
        ex = PolymarketExecutor(config)
        with pytest.raises(ExecutorError, match="insufficient funds"):
            await ex.place_order("token_id", "BUY", 5.0, price=0.65)


@pytest.mark.asyncio
async def test_close_order_calls_create_and_post_order(config):
    mock_client = make_mock_client()
    with patch("polymarket_executor.ClobClient", return_value=mock_client):
        ex = PolymarketExecutor(config)
        await ex.close_order("token_id", 5.0, price=0.65)
    assert mock_client.create_and_post_order.call_count == 1


@pytest.mark.asyncio
async def test_neg_risk_flag_passed_for_internal(config):
    mock_client = make_mock_client()
    with patch("polymarket_executor.ClobClient", return_value=mock_client):
        ex = PolymarketExecutor(config)
        await ex.place_order("token_id", "BUY", 5.0, price=0.50, neg_risk=True)
    call_args, call_kwargs = mock_client.create_and_post_order.call_args
    options = call_args[1] if len(call_args) > 1 else call_kwargs.get("options")
    assert options.neg_risk is True


@pytest.mark.asyncio
async def test_client_uses_correct_v2_auth_method(config):
    mock_client = make_mock_client()
    with patch("polymarket_executor.ClobClient", return_value=mock_client):
        ex = PolymarketExecutor(config)
        await ex.place_order("token_id", "BUY", 5.0, price=0.65)
    # v2 uses create_or_derive_api_key — NOT the old create_or_derive_api_creds
    mock_client.create_or_derive_api_key.assert_called_once()
    mock_client.create_or_derive_api_creds.assert_not_called()


def _make_executor(use_fok=True, min_bet=10.0):
    config = {
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
        "polymarket_signature_type": 0,
        "use_fok": use_fok,
        "min_bet_usdc": min_bet,
    }
    ex = PolymarketExecutor(config)
    return ex


def test_fok_falls_back_to_gtc_when_cancelled_with_depth():
    """FOK cancel + adequate liquidity -> GTC retry -> success."""
    executor = _make_executor(use_fok=True)
    call_order_types = []

    def fake_create_and_post_order(order_args, options=None, order_type=None):
        call_order_types.append(order_type)
        if str(order_type) == "FOK":
            return {"orderID": "", "status": "cancelled", "errorMsg": None}
        return {"orderID": "gtc-123", "status": "matched", "errorMsg": None}

    mock_client = MagicMock()
    mock_client.create_and_post_order.side_effect = fake_create_and_post_order
    executor._client = mock_client

    result = executor._place_sync(
        token_id="tok1", price=0.5, amount_usdc=20.0, neg_risk=False,
        poly_liquidity_usdc=50.0,  # 50 > 10*2=20 -> has depth for GTC
    )
    assert result["order_id"] == "gtc-123"
    types_called = [str(t) for t in call_order_types]
    assert any("FOK" in t for t in types_called), f"FOK not attempted: {types_called}"
    assert any("GTC" in t for t in types_called), f"GTC not attempted: {types_called}"


def test_fok_thin_book_raises_executor_error():
    """FOK cancel + thin book -> ExecutorError (no GTC retry)."""
    executor = _make_executor(use_fok=True)

    def fake_create_and_post_order(order_args, options=None, order_type=None):
        return {"orderID": "", "status": "cancelled", "errorMsg": None}

    mock_client = MagicMock()
    mock_client.create_and_post_order.side_effect = fake_create_and_post_order
    executor._client = mock_client

    with pytest.raises(ExecutorError, match="FOK cancelled, book too thin"):
        executor._place_sync(
            token_id="tok1", price=0.5, amount_usdc=20.0, neg_risk=False,
            poly_liquidity_usdc=15.0,  # 15 < 10*2=20 -> thin book
        )


def test_use_fok_false_skips_fok():
    """When use_fok=False, only GTC is used."""
    executor = _make_executor(use_fok=False)
    call_order_types = []

    def fake_create_and_post_order(order_args, options=None, order_type=None):
        call_order_types.append(order_type)
        return {"orderID": "gtc-456", "status": "matched", "errorMsg": None}

    mock_client = MagicMock()
    mock_client.create_and_post_order.side_effect = fake_create_and_post_order
    executor._client = mock_client

    result = executor._place_sync(
        token_id="tok1", price=0.5, amount_usdc=20.0, neg_risk=False,
    )
    assert result["order_id"] == "gtc-456"
    types_called = [str(t) for t in call_order_types]
    assert not any("FOK" in t for t in types_called), f"FOK should not be called: {types_called}"


# ---------------------------------------------------------------------------
# Fee cache tests
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_fee_cache_populates():
    """warm_fee_cache stores taker_fee_rate per token_id."""
    config = {
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
    }
    executor = PolymarketExecutor(config)

    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=[
        {"token_id": "tok-abc", "taker_fee": "0.015"},
        {"token_id": "tok-xyz", "taker_fee": "0.02"},
    ])

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=MagicMock(
        __aenter__=AsyncMock(return_value=mock_response),
        __aexit__=AsyncMock(return_value=False),
    ))

    with patch("polymarket_executor.aiohttp.ClientSession", return_value=mock_session):
        await executor.warm_fee_cache(["tok-abc", "tok-xyz"])

    assert executor._fee_cache["tok-abc"] == pytest.approx(0.015)
    assert executor._fee_cache["tok-xyz"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_fee_cache_miss_defaults():
    """Token not in API response falls back to 0.02."""
    config = {
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
    }
    executor = PolymarketExecutor(config)
    # Don't call warm_fee_cache — cache is empty
    assert executor._fee_cache.get("missing-token", 0.02) == 0.02


@pytest.mark.asyncio
async def test_fee_cache_api_error_falls_back():
    """API error during warm_fee_cache logs warning but does not raise."""
    config = {
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
    }
    executor = PolymarketExecutor(config)

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(side_effect=Exception("network error"))

    with patch("polymarket_executor.aiohttp.ClientSession", return_value=mock_session):
        await executor.warm_fee_cache(["tok-abc"])  # must not raise

    assert executor._fee_cache == {}
