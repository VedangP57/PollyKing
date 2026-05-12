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
