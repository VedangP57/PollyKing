import asyncio
import base64
import hashlib
import hmac as _hmac
import time
from typing import Optional

import aiohttp


class ExecutorError(Exception):
    pass


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
        """
        path = "/trade-api/v2/portfolio/orders"
        headers = self._sign("POST", path)
        body = {
            "ticker": ticker,
            "action": action,
            "count": int(count),
            "type": "market",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.api_url}/portfolio/orders",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if resp.status not in (200, 201):
                    raise ExecutorError(
                        f"Kalshi order failed HTTP {resp.status}: {data}"
                    )
                order = data.get("order", data)
                return {
                    "order_id": order.get("order_id", ""),
                    "status": order.get("status", ""),
                    "platform": "kalshi",
                    "ticker": ticker,
                    "count": count,
                }

    async def close_order(self, ticker: str, count: int) -> None:
        """Emergency close: sell back a filled position."""
        await self.place_order(ticker, "sell", count)
