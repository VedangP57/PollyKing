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


def _mock_resp(status: int, payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.json = AsyncMock(return_value=payload)
    return resp


def test_sign_produces_correct_headers(config):
    ex = KalshiExecutor(config)
    headers = ex._sign("POST", "/trade-api/v2/portfolio/orders")

    assert "Kalshi-Access-Key" in headers
    assert headers["Kalshi-Access-Key"] == "test_key"
    assert "Kalshi-Access-Signature" in headers
    assert "Kalshi-Access-Timestamp" in headers
    base64.b64decode(headers["Kalshi-Access-Signature"])


def test_sign_hmac_is_correct(config):
    ex = KalshiExecutor(config)
    method = "POST"
    path = "/trade-api/v2/portfolio/orders"
    headers = ex._sign(method, path)
    timestamp = headers["Kalshi-Access-Timestamp"]

    message = (timestamp + method + path).encode()
    expected_sig = base64.b64encode(
        hmac.new(config["kalshi_api_secret"].encode(), message, hashlib.sha256).digest()
    ).decode()

    assert headers["Kalshi-Access-Signature"] == expected_sig


@pytest.mark.asyncio
async def test_place_order_success(config):
    ex = KalshiExecutor(config)
    resp = _mock_resp(201, {"order": {"order_id": "ord_abc123", "status": "resting"}})

    with patch("kalshi_executor.async_retry_request", new=AsyncMock(return_value=resp)):
        result = await ex.place_order("KXBTCD-25MAY31-B95000", "buy", 20)

    assert result["order_id"] == "ord_abc123"
    assert result["status"] == "resting"


@pytest.mark.asyncio
async def test_place_order_raises_on_non_201(config):
    ex = KalshiExecutor(config)
    resp = _mock_resp(401, {"error": {"message": "auth failure"}})

    with patch("kalshi_executor.async_retry_request", new=AsyncMock(return_value=resp)):
        with pytest.raises(ExecutorError, match="401"):
            await ex.place_order("KXBTCD-25MAY31-B95000", "buy", 20)


@pytest.mark.asyncio
async def test_place_order_includes_client_order_id(config):
    ex = KalshiExecutor(config)
    resp = _mock_resp(201, {"order": {"order_id": "ord_xyz", "status": "resting"}})
    captured_body = {}

    async def capture_request(session, method, url, **kwargs):
        captured_body.update(kwargs.get("json", {}))
        return resp

    with patch("kalshi_executor.async_retry_request", side_effect=capture_request):
        await ex.place_order("TICKER-1", "buy", 5)

    assert "client_order_id" in captured_body
    assert len(captured_body["client_order_id"]) == 36  # UUID format


@pytest.mark.asyncio
async def test_place_order_409_fetches_existing_order(config):
    ex = KalshiExecutor(config)
    resp_409 = _mock_resp(409, {})

    existing_order = {
        "order_id": "ord_existing",
        "status": "resting",
        "ticker": "TICKER-1",
        "count": 5,
        "client_order_id": "will-be-matched",
    }
    resp_200 = _mock_resp(200, {"orders": [existing_order]})

    call_count = 0

    async def side_effect(session, method, url, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # POST — return 409
            resp_409.json = AsyncMock(return_value={})
            return resp_409
        # GET fallback — return list with matching order
        # Patch client_order_id into the order so the scan matches
        existing_order["client_order_id"] = kwargs.get("params", {}).get("client_order_id", "")
        return resp_200

    with patch("kalshi_executor.async_retry_request", side_effect=side_effect):
        result = await ex.place_order("TICKER-1", "buy", 5)

    assert result["order_id"] == "ord_existing"
    assert result["platform"] == "kalshi"
