"""
Polls open live trades and resolves them via Polymarket Gamma API or Kalshi REST API.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


@dataclass
class ResolutionResult:
    trade_id: int
    status: str          # "profit" | "loss" | "resolved"
    actual_profit: float


def compute_actual_profit(
    poly_side: str,
    kalshi_side: str,
    resolution: str,
    amount_usdc: float,
    gap_cents: float,
    fee_rate: float = 0.04,
) -> ResolutionResult:
    """Compute actual profit for a resolved two-leg arb trade.

    For guaranteed arb, the net profit is the same regardless of which leg wins.
    gross = (amount_usdc / combined) - amount_usdc
    net   = gross - fee_rate * amount_usdc
    """
    combined = 1.0 - gap_cents / 100.0
    k = amount_usdc / combined if combined > 0 else 0.0
    gross = k - amount_usdc
    fee = fee_rate * amount_usdc
    actual_profit = round(gross - fee, 4)
    status = "profit" if actual_profit >= 0 else "loss"
    return ResolutionResult(trade_id=0, status=status, actual_profit=actual_profit)


async def _fetch_market_status(session: aiohttp.ClientSession, gamma_id: str) -> Optional[dict]:
    """Fetch a single market from Polymarket Gamma API. Returns None on error."""
    try:
        url = f"{GAMMA_BASE}/markets/{gamma_id}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                return await resp.json()
    except Exception as e:
        log.debug(f"Gamma fetch error for {gamma_id}: {e}")
    return None


async def _fetch_kalshi_settlement(session: aiohttp.ClientSession, ticker: str) -> Optional[str]:
    """Fetch Kalshi market settlement result. Returns 'YES', 'NO', or None if unresolved."""
    try:
        url = f"{KALSHI_BASE}/markets/{ticker}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            market = data.get("market", {})
            if market.get("status") == "settled" and market.get("result"):
                return "YES" if market["result"].lower() == "yes" else "NO"
    except Exception as e:
        log.debug(f"Kalshi fetch error for {ticker}: {e}")
    return None


class Reconciler:
    def __init__(self, config: dict, db_conn):
        self.config = config
        self.db = db_conn
        self._poll_interval = float(config.get("reconcile_interval_s", 300.0))

    async def run_forever(self) -> None:
        """Background loop: reconcile open trades every poll_interval seconds."""
        async with aiohttp.ClientSession() as session:
            while True:
                await asyncio.sleep(self._poll_interval)
                await self._reconcile_once(session)

    async def _reconcile_once(self, session: aiohttp.ClientSession) -> None:
        from tracker import get_open_live_trades, resolve_trade
        trades = get_open_live_trades(self.db)
        if not trades:
            return

        for trade in trades:
            market_id = trade["market_id"]
            token_key = market_id.split("::")[0] if "::" in market_id else market_id

            pair_row = self.db.execute(
                "SELECT gamma_id_a, kalshi_ticker FROM market_pairs WHERE token_a=? OR token_b=?",
                (token_key, token_key),
            ).fetchone()

            resolution: Optional[str] = None
            source = ""

            # Try Polymarket Gamma first
            gamma_id = pair_row[0] if pair_row else None
            if gamma_id:
                data = await _fetch_market_status(session, gamma_id)
                if data and data.get("resolved"):
                    resolution = "YES" if data.get("resolutionPrice", 0) > 0.5 else "NO"
                    source = "polymarket"

            # Fall back to Kalshi if Polymarket not resolved yet
            if resolution is None:
                kalshi_ticker = pair_row[1] if pair_row else None
                if kalshi_ticker:
                    resolution = await _fetch_kalshi_settlement(session, kalshi_ticker)
                    if resolution:
                        source = "kalshi"

            if resolution is None:
                continue

            result = compute_actual_profit(
                poly_side=trade.get("polymarket_side", "NO"),
                kalshi_side=trade.get("kalshi_side", "YES"),
                resolution=resolution,
                amount_usdc=trade["amount_usdc"],
                gap_cents=float(trade.get("gap_cents") or 8.0),
                fee_rate=float(trade.get("fee_rate") or 0.04),
            )
            resolve_trade(self.db, trade["id"], result.actual_profit, result.status)
            log.info(
                f"Reconciled trade #{trade['id']} via {source}: "
                f"{result.status} ${result.actual_profit:.2f}"
            )
