import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
import requests

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
        "use_fok": True,
        "min_bet_usdc": 10.0,
    }


def make_mock_client(order_id="ord_poly_123", status="matched", error=""):
    """Build a mock ClobClient matching the official py_clob_client interface."""
    client = MagicMock()
    # L1 auth — official client uses create_or_derive_api_creds
    client.create_or_derive_api_creds.return_value = MagicMock()
    # Two-step order creation: create_market_order / create_order -> post_order
    client.create_market_order.return_value = MagicMock()  # signed order object
    client.create_order.return_value = MagicMock()
    client.post_order.return_value = {
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
    mock_client.create_market_order.assert_called_once()


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
    # close uses create_order + post_order (GTC SELL)
    mock_client.create_order.assert_called_once()
    mock_client.post_order.assert_called_once()


@pytest.mark.asyncio
async def test_neg_risk_flag_passed_for_internal(config):
    mock_client = make_mock_client()
    with patch("polymarket_executor.ClobClient", return_value=mock_client):
        ex = PolymarketExecutor(config)
        await ex.place_order("token_id", "BUY", 5.0, price=0.50, neg_risk=True)
    # FOK path: create_market_order called with options
    call_args = mock_client.create_market_order.call_args
    options = call_args[0][1] if call_args[0] and len(call_args[0]) > 1 else call_args[1].get("options")
    assert options is not None and options.neg_risk is True


@pytest.mark.asyncio
async def test_client_uses_official_auth_method(config):
    mock_client = make_mock_client()
    with patch("polymarket_executor.ClobClient", return_value=mock_client):
        ex = PolymarketExecutor(config)
        await ex.place_order("token_id", "BUY", 5.0, price=0.65)
    # Official client uses create_or_derive_api_creds (not the old create_or_derive_api_key)
    mock_client.create_or_derive_api_creds.assert_called_once()


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
    mock_client = MagicMock()
    mock_client.create_market_order.return_value = MagicMock()
    mock_client.create_order.return_value = MagicMock()

    post_call_count = [0]

    def fake_post_order(order, order_type=None):
        post_call_count[0] += 1
        from py_clob_client.clob_types import OrderType
        if order_type == OrderType.FOK:
            return {"orderID": "", "status": "cancelled", "errorMsg": None}
        return {"orderID": "gtc-123", "status": "matched", "errorMsg": None}

    mock_client.post_order.side_effect = fake_post_order
    executor._client = mock_client

    result = executor._place_sync(
        token_id="tok1", price=0.5, amount_usdc=20.0, neg_risk=False,
        poly_liquidity_usdc=50.0,
    )
    assert result["order_id"] == "gtc-123"
    assert post_call_count[0] == 2  # FOK then GTC


def test_fok_thin_book_raises_executor_error():
    """FOK cancel + thin book -> ExecutorError (no GTC retry)."""
    executor = _make_executor(use_fok=True)
    mock_client = MagicMock()
    mock_client.create_market_order.return_value = MagicMock()
    mock_client.post_order.return_value = {"orderID": "", "status": "cancelled", "errorMsg": None}
    executor._client = mock_client

    with pytest.raises(ExecutorError, match="FOK cancelled, book too thin"):
        executor._place_sync(
            token_id="tok1", price=0.5, amount_usdc=20.0, neg_risk=False,
            poly_liquidity_usdc=15.0,
        )


def test_use_fok_false_skips_fok():
    """When use_fok=False, only GTC is used (create_order, not create_market_order)."""
    executor = _make_executor(use_fok=False)
    mock_client = MagicMock()
    mock_client.create_order.return_value = MagicMock()
    mock_client.post_order.return_value = {"orderID": "gtc-456", "status": "matched", "errorMsg": None}
    executor._client = mock_client

    result = executor._place_sync(token_id="tok1", price=0.5, amount_usdc=20.0, neg_risk=False)
    assert result["order_id"] == "gtc-456"
    mock_client.create_market_order.assert_not_called()


def test_place_sync_retries_on_429():
    """HTTP 429 from Polymarket -> retry up to 3 times before raising ExecutorError."""
    executor = _make_executor(use_fok=True)
    mock_client = MagicMock()
    mock_client.create_market_order.return_value = MagicMock()

    rate_limit_resp = MagicMock()
    rate_limit_resp.status_code = 429
    err = requests.HTTPError(response=rate_limit_resp)
    mock_client.post_order.side_effect = err
    executor._client = mock_client

    with patch("polymarket_executor.time.sleep"):
        with pytest.raises(ExecutorError, match="max retries exhausted"):
            executor._place_sync("tok1", 0.5, 20.0)

    assert mock_client.create_market_order.call_count == 3


# ---------------------------------------------------------------------------
# Fee cache tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fee_cache_populates():
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
    config = {
        "polymarket_private_key": "0x" + "a" * 64,
        "polymarket_wallet_address": "0x" + "b" * 40,
    }
    executor = PolymarketExecutor(config)
    assert executor._fee_cache.get("missing-token", 0.02) == 0.02


@pytest.mark.asyncio
async def test_fee_cache_api_error_falls_back():
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
