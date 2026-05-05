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
        "dry_run": False,
    }


def make_mock_client(order_id="ord_poly_123", status="matched", error=""):
    client = MagicMock()
    client.create_or_derive_api_creds.return_value = MagicMock()
    client.create_market_order.return_value = MagicMock()
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
        )
    assert result["order_id"] == "ord_poly_123"
    assert result["status"] == "matched"
    assert result["platform"] == "polymarket"


@pytest.mark.asyncio
async def test_place_order_raises_on_error_msg(config):
    mock_client = make_mock_client(error="insufficient funds")
    with patch("polymarket_executor.ClobClient", return_value=mock_client):
        ex = PolymarketExecutor(config)
        with pytest.raises(ExecutorError, match="insufficient funds"):
            await ex.place_order("token_id", "BUY", 5.0)


@pytest.mark.asyncio
async def test_close_order_calls_place_order(config):
    mock_client = make_mock_client()
    with patch("polymarket_executor.ClobClient", return_value=mock_client):
        ex = PolymarketExecutor(config)
        # Should not raise
        await ex.close_order("token_id", 5.0)
    assert mock_client.post_order.call_count == 1
