import asyncio
import logging
import sqlite3
from typing import Optional

from kalshi_executor import ExecutorError, KalshiExecutor
from kelly_engine import compute_arb_kelly_size
from polymarket_executor import PolymarketExecutor
from tracker import log_emergency_position

log = logging.getLogger(__name__)


class TwoLegExecutor:
    """Fires both legs of an arb trade concurrently.

    Cross-platform: Polymarket (NO) + Kalshi (YES).
    Internal:       Polymarket token_a (YES) + Polymarket token_b (YES).

    On partial fill, immediately emergency-closes the filled leg and records
    the position in the emergency_positions table for manual review.
    """

    def __init__(self, config: dict, db_conn: sqlite3.Connection):
        self._config = config
        self._db = db_conn
        self._poly = PolymarketExecutor(config)
        self._kalshi = KalshiExecutor(config)

    def _compute_bet_size(self, gap: dict) -> float:
        bankroll = self._config.get("bankroll_usdc", 500.0)
        fraction = self._config.get("kelly_fraction", 0.25)
        min_bet = self._config.get("min_bet_usdc", 10.0)
        max_bet = self._config.get("max_bet_usdc", 100.0)
        pair_type = gap.get("pair_type", "cross_platform")
        poly_price = gap["polymarket_price"]
        kalshi_price = gap["kalshi_price"]
        combined = (
            poly_price + kalshi_price
            if pair_type == "internal"
            else (1.0 - poly_price) + kalshi_price
        )
        result = compute_arb_kelly_size(
            bankroll=bankroll,
            combined=combined,
            confidence=gap.get("confidence", "medium"),
            fraction=fraction,
            max_bet_pct=0.05,
            min_bet_usdc=min_bet,
            max_bet_usdc=max_bet,
        )
        return result["bet_usdc"] if result["action"] == "BET" else min_bet

    async def execute(self, gap: dict, bet_size: Optional[float] = None) -> Optional[dict]:
        if bet_size is None:
            bet_size = self._compute_bet_size(gap)

        pair_type = gap.get("pair_type", "cross_platform")
        if pair_type == "internal":
            return await self._execute_internal(gap, bet_size)
        return await self._execute_cross_platform(gap, bet_size)

    async def _execute_cross_platform(self, gap: dict, bet_size: float) -> Optional[dict]:
        poly_price = gap["polymarket_price"]
        kalshi_price = gap["kalshi_price"]
        combined = (1.0 - poly_price) + kalshi_price
        k = bet_size / combined if combined > 0 else 0.0
        poly_amount = round(k * (1.0 - poly_price), 4)
        kalshi_count = max(1, round(k))

        poly_task = self._poly.place_order(
            token_id=gap["polymarket_token"],
            side="BUY",
            amount_usdc=poly_amount,
        )
        kalshi_task = self._kalshi.place_order(
            ticker=gap["kalshi_ticker"],
            action="buy",
            count=kalshi_count,
        )
        return await self._gather_legs(
            gap, poly_task, kalshi_task, bet_size=bet_size,
            poly_amount=poly_amount, kalshi_count=kalshi_count,
        )

    async def _execute_internal(self, gap: dict, bet_size: float) -> Optional[dict]:
        poly_price = gap["polymarket_price"]
        kalshi_price = gap["kalshi_price"]
        combined = poly_price + kalshi_price
        k = bet_size / combined if combined > 0 else 0.0
        amount_a = round(k * poly_price, 4)
        amount_b = round(k * kalshi_price, 4)

        task_a = self._poly.place_order(
            token_id=gap["polymarket_token"],
            side="BUY",
            amount_usdc=amount_a,
        )
        task_b = self._poly.place_order(
            token_id=gap["kalshi_ticker"],  # token_b stored in kalshi_ticker for internal pairs
            side="BUY",
            amount_usdc=amount_b,
        )
        return await self._gather_legs(
            gap, task_a, task_b, bet_size=bet_size,
            poly_amount=amount_a, kalshi_count=None,
        )

    async def _gather_legs(
        self,
        gap: dict,
        task_a,
        task_b,
        bet_size: float,
        poly_amount: float,
        kalshi_count: Optional[int],
    ) -> Optional[dict]:
        result_a, result_b = await asyncio.gather(task_a, task_b, return_exceptions=True)

        a_ok = not isinstance(result_a, Exception)
        b_ok = not isinstance(result_b, Exception)

        if a_ok and b_ok:
            fee_rate = gap.get("fee_rate", 0.04)
            combined = (
                gap["polymarket_price"] + gap["kalshi_price"]
                if gap.get("pair_type") == "internal"
                else (1.0 - gap["polymarket_price"]) + gap["kalshi_price"]
            )
            k = bet_size / combined if combined > 0 else 0.0
            fee = fee_rate * bet_size
            expected_profit = round(k - bet_size - fee, 4)
            return {
                "polymarket_order_id": result_a.get("order_id", ""),
                "kalshi_order_id": result_b.get("order_id", ""),
                "total_spent": round(bet_size, 4),
                "gap_cents": gap.get("gap_cents", 0.0),
                "expected_profit": expected_profit,
                "dry_run": False,
            }

        if not a_ok and not b_ok:
            log.warning(
                "Both legs failed for %s — a: %s | b: %s",
                gap["market_id"], result_a, result_b,
            )
            return None

        # Partial fill — emergency close the filled leg
        filled_result = result_a if a_ok else result_b
        failed_error = result_b if a_ok else result_a
        log.error(
            "PARTIAL FILL on %s — filled: %s | failed: %s — emergency closing",
            gap["market_id"], filled_result.get("order_id"), failed_error,
        )
        await self._emergency_close(filled_result, gap)
        return None

    async def _emergency_close(self, filled: dict, gap: dict) -> None:
        platform = filled.get("platform", "")
        try:
            if platform == "polymarket":
                await self._poly.close_order(
                    token_id=filled["token_id"],
                    amount_usdc=filled["amount_usdc"],
                )
            elif platform == "kalshi":
                await self._kalshi.close_order(
                    ticker=filled["ticker"],
                    count=filled["count"],
                )
            status = "closed_auto"
        except Exception as e:
            log.error(
                "Emergency close FAILED for %s: %s — REQUIRES MANUAL ACTION",
                filled.get("order_id"), e,
            )
            status = "open"

        log_emergency_position(self._db, {
            "market_id": gap.get("market_id", ""),
            "platform": platform,
            "order_id": filled.get("order_id", ""),
            "side": filled.get("ticker") or filled.get("token_id", ""),
            "amount_usdc": filled.get("amount_usdc", 0.0),
        })
        if status == "closed_auto":
            self._db.execute(
                "UPDATE emergency_positions SET status='closed_auto', closed_at=datetime('now') "
                "WHERE order_id=?",
                (filled.get("order_id", ""),),
            )
            self._db.commit()
