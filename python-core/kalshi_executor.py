import asyncio
import base64
import hashlib
import hmac as _hmac
import time
import uuid
from typing import Optional

import aiohttp

from http_utils import ExecutorError, async_retry_request

__all__ = ["ExecutorError", "KalshiExecutor"]


class KalshiExecutor:
    """Places live orders on Kalshi via their REST API with HMAC-SHA256 signing.

    count = number of contracts (integer).
    Caller computes: count = round(bet_size / combined) where combined = price_a + price_b.
    """

    def __init__(self, config: dict):
        self.api_key: str = config["kalshi_api_key"]
        self.api_secret: str = config["kalshi_api_secret"]
        self.api_url: str = config.get(
            "kalshi_api_url",
            "https://api.elections.kalshi.com/trade-api/v2",
        )

    def _sign(self, method: str, path: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        message = (timestamp + method + path).encode()
        signature = base64.b64encode(
            _hmac.new(self.api_secret.encode(), message, hashlib.sha256).digest()
        ).decode()
        return {
            "Authorization": f"Token {self.api_key}",
            "Kalshi-Access-Key": self.api_key,
            "Kalshi-Access-Signature": signature,
            "Kalshi-Access-Timestamp": timestamp,
            "Content-Type": "application/json",
        }

    async def place_order(self, ticker: str, action: str, count: int) -> dict:
        """Place a market order on Kalshi.

        ticker: Kalshi market ticker e.g. "KXBTCD-25MAY31-B95000"
        action: "buy" or "sell"
        count:  integer number of contracts

        Idempotent: a UUID client_order_id is sent with each request. On HTTP 409
        (duplicate key) the existing order is fetched and returned unchanged.
        """
        client_order_id = str(uuid.uuid4())
        path = "/trade-api/v2/portfolio/orders"
        headers = self._sign("POST", path)
        body = {
            "ticker": ticker,
            "action": action,
            "count": int(count),
            "type": "market",
            "client_order_id": client_order_id,
        }
        async with aiohttp.ClientSession() as session:
            resp = await async_retry_request(
                session, "POST", f"{self.api_url}/portfolio/orders",
                json=body, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status == 409:
                return await self._fetch_by_client_order_id(session, client_order_id)
            data = await resp.json()
            if resp.status not in (200, 201):
                raise ExecutorError(f"Kalshi order failed HTTP {resp.status}: {data}")
            order = data.get("order", data)
            return {
                "order_id": order.get("order_id", ""),
                "status": order.get("status", ""),
                "platform": "kalshi",
                "ticker": ticker,
                "count": count,
            }

    async def _fetch_by_client_order_id(
        self, session: aiohttp.ClientSession, client_order_id: str
    ) -> dict:
        """Return the existing order that matches client_order_id after a 409 conflict."""
        path = "/trade-api/v2/portfolio/orders"
        headers = self._sign("GET", path)
        resp = await async_retry_request(
            session, "GET", f"{self.api_url}/portfolio/orders",
            headers=headers,
            params={"client_order_id": client_order_id},
            timeout=aiohttp.ClientTimeout(total=10),
        )
        if resp.status == 200:
            data = await resp.json()
            orders = data.get("orders", [])
            for o in orders:
                if o.get("client_order_id") == client_order_id:
                    return {
                        "order_id": o.get("order_id", ""),
                        "status": o.get("status", ""),
                        "platform": "kalshi",
                        "ticker": o.get("ticker", ""),
                        "count": o.get("count", 0),
                    }
        # Fallback: API didn't match by client_order_id — return a sentinel so caller
        # can decide whether to treat this as an error.
        raise ExecutorError(f"Kalshi 409 conflict but could not retrieve order (client_order_id={client_order_id})")

    async def close_order(self, ticker: str, count: int) -> None:
        """Emergency close: sell back a filled position."""
        await self.place_order(ticker, "sell", count)

    async def get_open_orders(self) -> list[dict]:
        """Return all open orders from Kalshi portfolio."""
        path = "/trade-api/v2/portfolio/orders"
        headers = self._sign("GET", path)
        async with aiohttp.ClientSession() as session:
            resp = await async_retry_request(
                session, "GET", f"{self.api_url}/portfolio/orders",
                headers=headers,
                params={"status": "open"},
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status != 200:
                return []
            data = await resp.json()
            return data.get("orders", [])

    async def get_order_status(self, order_id: str) -> str:
        """Return Kalshi order status string: 'resting', 'matched', 'canceled', etc."""
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        headers = self._sign("GET", path)
        async with aiohttp.ClientSession() as session:
            resp = await async_retry_request(
                session, "GET", f"{self.api_url}/portfolio/orders/{order_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status != 200:
                return "unknown"
            data = await resp.json()
            return data.get("order", {}).get("status", "unknown")

    async def cancel_order(self, order_id: str) -> None:
        """Cancel an open Kalshi order by order_id."""
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        headers = self._sign("DELETE", path)
        async with aiohttp.ClientSession() as session:
            resp = await async_retry_request(
                session, "DELETE", f"{self.api_url}/portfolio/orders/{order_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status not in (200, 204):
                raise ExecutorError(f"Kalshi cancel failed HTTP {resp.status}")

    async def get_fill_details(self, order_id: str) -> Optional[float]:
        """Return the average fill price for a completed Kalshi order, or None on error."""
        path = f"/trade-api/v2/portfolio/orders/{order_id}"
        headers = self._sign("GET", path)
        async with aiohttp.ClientSession() as session:
            resp = await async_retry_request(
                session, "GET", f"{self.api_url}/portfolio/orders/{order_id}",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            )
            if resp.status != 200:
                return None
            data = await resp.json()
            order = data.get("order", {})
            avg_price = order.get("avg_price") or order.get("yes_price") or order.get("price")
            try:
                return float(avg_price) / 100.0 if avg_price is not None else None
            except (TypeError, ValueError):
                return None
