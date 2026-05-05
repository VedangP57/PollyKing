import base64
import hashlib
import hmac
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from kalshi_executor import KalshiExecutor, ExecutorError


@pytest.fixture
def config():
    return {
        "kalshi_api_key": "test_key",
        "kalshi_api_secret": "test_secret_32byteslong_padding!",
        "kalshi_api_url": "https://api.elections.kalshi.com/trade-api/v2",
        "dry_run": False,
    }


def test_sign_produces_correct_headers(config):
    ex = KalshiExecutor(config)
    headers = ex._sign("POST", "/trade-api/v2/portfolio/orders")

    assert "Kalshi-Access-Key" in headers
    assert headers["Kalshi-Access-Key"] == "test_key"
    assert "Kalshi-Access-Signature" in headers
    assert "Kalshi-Access-Timestamp" in headers
    # Signature must be valid base64
    base64.b64decode(headers["Kalshi-Access-Signature"])


def test_sign_hmac_is_correct(config):
    ex = KalshiExecutor(config)
    method = "POST"
    path = "/trade-api/v2/portfolio/orders"
    headers = ex._sign(method, path)
    timestamp = headers["Kalshi-Access-Timestamp"]

    # Recompute expected signature
    message = (timestamp + method + path).encode()
    expected_sig = base64.b64encode(
        hmac.new(config["kalshi_api_secret"].encode(), message, hashlib.sha256).digest()
    ).decode()

    assert headers["Kalshi-Access-Signature"] == expected_sig


@pytest.mark.asyncio
async def test_place_order_success(config):
    ex = KalshiExecutor(config)
    mock_response = MagicMock()
    mock_response.status = 201
    mock_response.json = AsyncMock(return_value={
        "order": {"order_id": "ord_abc123", "status": "resting"}
    })
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("kalshi_executor.aiohttp.ClientSession", return_value=mock_session):
        result = await ex.place_order("KXBTCD-25MAY31-B95000", "buy", 20)

    assert result["order_id"] == "ord_abc123"
    assert result["status"] == "resting"


@pytest.mark.asyncio
async def test_place_order_raises_on_non_201(config):
    ex = KalshiExecutor(config)
    mock_response = MagicMock()
    mock_response.status = 401
    mock_response.json = AsyncMock(return_value={"error": {"message": "auth failure"}})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("kalshi_executor.aiohttp.ClientSession", return_value=mock_session):
        with pytest.raises(ExecutorError, match="401"):
            await ex.place_order("KXBTCD-25MAY31-B95000", "buy", 20)
