import asyncio
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.constants import POLYGON

from kalshi_executor import ExecutorError


class PolymarketExecutor:
    """Places live orders on Polymarket via py_clob_client.

    py_clob_client is synchronous — orders run in a thread executor so they
    don't block the asyncio event loop.
    """

    def __init__(self, config: dict):
        self._config = config
        self._client: Optional[ClobClient] = None

    def _get_client(self) -> ClobClient:
        if self._client is None:
            self._client = ClobClient(
                host="https://clob.polymarket.com",
                key=self._config["polymarket_private_key"],
                chain_id=POLYGON,
                signature_type=0,
                funder=self._config.get("polymarket_wallet_address", ""),
            )
            api_creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(api_creds)
        return self._client

    def _place_sync(self, token_id: str, amount_usdc: float) -> dict:
        client = self._get_client()
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usdc,
            side="BUY",
        )
        signed_order = client.create_market_order(order_args)
        resp = client.post_order(signed_order, OrderType.FOK)
        if isinstance(resp, dict) and resp.get("errorMsg"):
            raise ExecutorError(f"Polymarket order rejected: {resp['errorMsg']}")
        return {
            "order_id": resp.get("orderID", ""),
            "status": resp.get("status", ""),
            "platform": "polymarket",
            "token_id": token_id,
            "amount_usdc": amount_usdc,
        }

    def _close_sync(self, token_id: str, amount_usdc: float) -> None:
        client = self._get_client()
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usdc,
            side="SELL",
        )
        signed_order = client.create_market_order(order_args)
        client.post_order(signed_order, OrderType.FOK)

    async def place_order(self, token_id: str, side: str, amount_usdc: float) -> dict:
        """Place a market BUY order on Polymarket. side param is accepted but always BUY."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._place_sync, token_id, amount_usdc)

    async def close_order(self, token_id: str, amount_usdc: float) -> None:
        """Emergency close: sell back a filled position."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._close_sync, token_id, amount_usdc)
