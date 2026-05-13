import asyncio

import aiohttp

_BACKOFF = [1, 2, 4]  # seconds between retries


class ExecutorError(Exception):
    pass


async def async_retry_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    **kwargs,
) -> aiohttp.ClientResponse:
    """Make an HTTP request, retrying on 429 with exponential backoff.

    Returns the ClientResponse on success. Raises ExecutorError if all
    retries are exhausted. All non-429 errors propagate immediately.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        resp = await session.request(method, url, **kwargs)
        if resp.status != 429:
            return resp
        retry_after = _parse_retry_after(resp.headers, _BACKOFF[min(attempt, len(_BACKOFF) - 1)])
        await asyncio.sleep(retry_after)
        last_exc = ExecutorError(f"HTTP 429 from {url} after {attempt + 1} attempt(s)")

    raise last_exc or ExecutorError(f"HTTP 429 from {url}: max retries exhausted")


def _parse_retry_after(headers: "aiohttp.CIMultiDictProxy", default: float) -> float:
    raw = headers.get("Retry-After")
    if raw is None:
        return default
    try:
        return max(float(raw), 0.1)
    except ValueError:
        return default
