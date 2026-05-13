import asyncio
import logging
import time
from typing import Optional

import aiohttp
import requests

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType,
    PartialCreateOrderOptions,
)
from py_clob_client.order_builder.constants import BUY, SELL

from kalshi_executor import ExecutorError

log = logging.getLogger(__name__)

# Signature types per Polymarket docs:
#   0 = EOA  (standard private-key wallet — use this for programmatic/API trading)
#   1 = POLY_PROXY  (Magic Link / Google login export)
#   2 = GNOSIS_SAFE (proxy wallet shown on polymarket.com/settings — most browser users)
_DEFAULT_SIG_TYPE = 0
_PLACE_MAX_RETRIES = 3


class PolymarketExecutor:
    """Places live orders on Polymarket via py_clob_client (official Polymarket package).

    Two-phase auth per Polymarket docs:
      Phase 1 (L1): sign with private key → derive API credentials
      Phase 2 (L2): use derived creds for all trading requests

    py_clob_client is synchronous — orders run in a thread executor so they
    don't block the asyncio event loop.
    """

    def __init__(self, config: dict):
        self._config = config
        self._client: Optional[ClobClient] = None
        self._fee_cache: dict[str, float] = {}

    async def warm_fee_cache(self, token_ids: list[str]) -> None:
        """Fetch taker fee rate for each token from CLOB API. Results cached in self._fee_cache."""
        if not token_ids:
            return
        try:
            async with aiohttp.ClientSession() as session:
                params = [("token_id", tid) for tid in token_ids]
                async with session.get(
                    "https://clob.polymarket.com/markets",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        log.warning("Fee cache: CLOB /markets returned %d — using defaults", resp.status)
                        return
                    data = await resp.json()
                    for market in data if isinstance(data, list) else []:
                        tid = market.get("token_id", "")
                        fee_str = market.get("taker_fee", "")
                        if tid and fee_str:
                            try:
                                self._fee_cache[tid] = float(fee_str)
                            except (ValueError, TypeError):
                                pass
            missing = set(token_ids) - set(self._fee_cache)
            if missing:
                log.warning(
                    "Fee cache: %d/%d tokens not returned by CLOB API — will use defaults: %s",
                    len(missing), len(token_ids), missing,
                )
            log.info("Fee cache warmed: %d tokens", len(self._fee_cache))
        except Exception as e:
            log.warning("Fee cache warm-up failed (%s) — using flat default 0.02 for all tokens", e)

    def _get_client(self) -> ClobClient:
        if self._client is not None:
            return self._client

        key = self._config["polymarket_private_key"]
        funder = self._config.get("polymarket_wallet_address", "")
        sig_type = int(self._config.get("polymarket_signature_type", _DEFAULT_SIG_TYPE))

        # Phase 1: derive L2 API credentials using private key (L1 auth)
        l1_client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=key,
        )
        creds = l1_client.create_or_derive_api_creds()

        # Phase 2: full client with L2 credentials enabled
        self._client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=key,
            creds=creds,
            signature_type=sig_type,
            funder=funder,
        )
        return self._client

    def _place_sync(
        self,
        token_id: str,
        price: float,
        amount_usdc: float,
        neg_risk: bool = False,
        poly_liquidity_usdc: float = float("inf"),
    ) -> dict:
        client = self._get_client()
        size = round(amount_usdc / price, 2) if price > 0 else 0.0
        options = PartialCreateOrderOptions(tick_size="0.01", neg_risk=neg_risk)
        use_fok = self._config.get("use_fok", True)

        if use_fok:
            fok_resp = self._post_fok_with_retry(client, token_id, price, size, options)
            if isinstance(fok_resp, dict) and fok_resp.get("errorMsg"):
                raise ExecutorError(f"Polymarket FOK rejected: {fok_resp['errorMsg']}")
            fok_status = fok_resp.get("status", "") if isinstance(fok_resp, dict) else ""
            fok_id = fok_resp.get("orderID", "") if isinstance(fok_resp, dict) else ""
            if fok_id and fok_status not in ("cancelled",):
                return {
                    "order_id": fok_id,
                    "status": fok_status,
                    "platform": "polymarket",
                    "token_id": token_id,
                    "amount_usdc": amount_usdc,
                }
            # FOK cancelled — check book depth before GTC fallback
            min_bet = self._config.get("min_bet_usdc", 10.0)
            if poly_liquidity_usdc <= min_bet * 2:
                raise ExecutorError("FOK cancelled, book too thin — not retrying with GTC")

        order_args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=size,
            side=BUY,
        )
        resp = self._post_gtc_with_retry(client, order_args, options)
        if isinstance(resp, dict) and resp.get("errorMsg"):
            raise ExecutorError(f"Polymarket order rejected: {resp['errorMsg']}")
        return {
            "order_id": resp.get("orderID", ""),
            "status": resp.get("status", ""),
            "platform": "polymarket",
            "token_id": token_id,
            "amount_usdc": amount_usdc,
        }

    def _post_fok_with_retry(
        self,
        client: ClobClient,
        token_id: str,
        price: float,
        size: float,
        options: PartialCreateOrderOptions,
    ) -> dict:
        """Build and post a FOK order, retrying once on HTTP 429."""
        market_args = MarketOrderArgs(
            token_id=token_id,
            amount=size,
            side=BUY,
            price=round(price, 4),
            order_type=OrderType.FOK,
        )
        for attempt in range(_PLACE_MAX_RETRIES):
            try:
                order = client.create_market_order(market_args, options)
                return client.post_order(order, OrderType.FOK)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise ExecutorError("Polymarket FOK: max retries exhausted after repeated 429s")

    def _post_gtc_with_retry(
        self,
        client: ClobClient,
        order_args: OrderArgs,
        options: PartialCreateOrderOptions,
    ) -> dict:
        """Build and post a GTC order, retrying once on HTTP 429."""
        for attempt in range(_PLACE_MAX_RETRIES):
            try:
                order = client.create_order(order_args, options)
                return client.post_order(order, OrderType.GTC)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                raise
        raise ExecutorError("Polymarket GTC: max retries exhausted after repeated 429s")

    def _close_sync(
        self,
        token_id: str,
        price: float,
        amount_usdc: float,
        neg_risk: bool = False,
    ) -> None:
        client = self._get_client()
        sell_price = max(round(price - 0.01, 4), 0.01)
        size = round(amount_usdc / price, 2) if price > 0 else 0.0
        order_args = OrderArgs(
            token_id=token_id,
            price=sell_price,
            size=size,
            side=SELL,
        )
        options = PartialCreateOrderOptions(tick_size="0.01", neg_risk=neg_risk)
        order = client.create_order(order_args, options)
        client.post_order(order, OrderType.GTC)

    def _get_balance_sync(self) -> float:
        """Return available USDC balance from Polymarket CLOB API."""
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        client = self._get_client()
        resp = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        if isinstance(resp, dict):
            raw = resp.get("balance", "0")
            try:
                return float(raw) / 1_000_000
            except (TypeError, ValueError):
                return 0.0
        return 0.0

    async def get_balance(self) -> float:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._get_balance_sync)

    def _get_positions_sync(self) -> list[dict]:
        """Return open orders (active positions) from Polymarket CLOB API."""
        client = self._get_client()
        resp = client.get_orders(OpenOrderParams())
        if isinstance(resp, list):
            return resp
        return []

    async def get_open_positions(self) -> list[dict]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._get_positions_sync)

    def _get_order_status_sync(self, order_id: str) -> str:
        """Return Polymarket order status: 'matched', 'open', 'canceled', etc."""
        client = self._get_client()
        resp = client.get_order(order_id)
        if isinstance(resp, dict):
            return resp.get("status", resp.get("orderStatus", "unknown"))
        return "unknown"

    def _cancel_order_sync(self, order_id: str) -> None:
        client = self._get_client()
        client.cancel(order_id)

    async def get_order_status(self, order_id: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._get_order_status_sync, order_id)

    async def cancel_order(self, order_id: str) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._cancel_order_sync, order_id)

    async def place_order(
        self,
        token_id: str,
        side: str,
        amount_usdc: float,
        price: float = 0.5,
        neg_risk: bool = False,
        poly_liquidity_usdc: float = float("inf"),
    ) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._place_sync, token_id, price, amount_usdc, neg_risk, poly_liquidity_usdc
        )

    async def close_order(
        self,
        token_id: str,
        amount_usdc: float,
        price: float = 0.5,
        neg_risk: bool = False,
    ) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self._close_sync, token_id, price, amount_usdc, neg_risk
        )

    def _get_fill_details_sync(self, order_id: str) -> Optional[float]:
        """Return average fill price for a matched Polymarket order, or None on error."""
        try:
            client = self._get_client()
            resp = client.get_order(order_id)
            if isinstance(resp, dict):
                avg = resp.get("avgPrice") or resp.get("price") or resp.get("orderPrice")
                return float(avg) if avg is not None else None
        except Exception:
            return None
        return None

    async def get_fill_details(self, order_id: str) -> Optional[float]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._get_fill_details_sync, order_id)
