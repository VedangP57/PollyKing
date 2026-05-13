import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from kalshi_executor import ExecutorError
from http_utils import async_retry_request


def _make_response(status: int, headers: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.headers = headers or {}
    return resp


@pytest.mark.asyncio
async def test_success_on_first_try():
    resp = _make_response(200)
    session = MagicMock()
    session.request = AsyncMock(return_value=resp)

    result = await async_retry_request(session, "GET", "https://example.com")

    assert result.status == 200
    assert session.request.call_count == 1


@pytest.mark.asyncio
async def test_429_then_success_retries():
    resp_429 = _make_response(429, {"Retry-After": "0"})
    resp_200 = _make_response(200)
    session = MagicMock()
    session.request = AsyncMock(side_effect=[resp_429, resp_200])

    with patch("http_utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        result = await async_retry_request(session, "GET", "https://example.com")

    assert result.status == 200
    assert session.request.call_count == 2
    mock_sleep.assert_called_once_with(0.1)  # Retry-After: 0 → clamped to 0.1


@pytest.mark.asyncio
async def test_429_uses_backoff_when_no_retry_after_header():
    resp_429 = _make_response(429)  # no Retry-After header
    resp_200 = _make_response(200)
    session = MagicMock()
    session.request = AsyncMock(side_effect=[resp_429, resp_200])

    with patch("http_utils.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await async_retry_request(session, "GET", "https://example.com")

    mock_sleep.assert_called_once_with(1)  # first backoff = 1s


@pytest.mark.asyncio
async def test_exhausted_retries_raises_executor_error():
    resp_429 = _make_response(429, {"Retry-After": "0"})
    session = MagicMock()
    session.request = AsyncMock(return_value=resp_429)

    with patch("http_utils.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(ExecutorError):
            await async_retry_request(session, "GET", "https://example.com", max_retries=3)

    assert session.request.call_count == 3


@pytest.mark.asyncio
async def test_non_429_error_propagates_immediately():
    resp_500 = _make_response(500)
    session = MagicMock()
    session.request = AsyncMock(return_value=resp_500)

    result = await async_retry_request(session, "GET", "https://example.com")

    assert result.status == 500
    assert session.request.call_count == 1  # no retry on non-429
