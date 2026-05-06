import asyncio
import logging
import sqlite3
from typing import Optional

from execution_policy import decide as _policy_decide
from kalshi_executor import ExecutorError, KalshiExecutor
from kelly_engine import compute_arb_kelly_size
from polymarket_executor import PolymarketExecutor
from tracker import log_emergency_position

log = logging.getLogger(__name__)

_FILL_POLL_INTERVAL = 2.0   # seconds between status polls
_FILL_TIMEOUT = 30.0        # seconds before treating order as unfilled


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

    async def _wait_for_fill(self, platform: str, order_id: str) -> bool:
        """Poll order status until filled or timeout. Returns True if filled."""
        deadline = asyncio.get_event_loop().time() + _FILL_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            try:
                if platform == "polymarket":
                    status = await self._poly.get_order_status(order_id)
                elif platform == "kalshi":
                    status = await self._kalshi.get_order_status(order_id)
                else:
                    return False
                if status == "matched":
                    return True
                if status in ("canceled", "cancelled"):
                    return False
            except Exception as e:
                log.debug("Fill poll error for %s %s: %s", platform, order_id, e)
            await asyncio.sleep(_FILL_POLL_INTERVAL)
        return False

    def _compute_bet_size(self, gap: dict) -> float:
        bankroll = self._config.get("bankroll_usdc", 500.0)
        fraction = self._config.get("kelly_fraction", 0.25)
        min_bet = self._config.get("min_bet_usdc", 10.0)
        max_bet = self._config.get("max_bet_usdc", 100.0)
        poly_price = gap["polymarket_price"]
        kalshi_price = gap["kalshi_price"]
        # Both prices are now the actual leg prices — just add them directly
        combined = poly_price + kalshi_price
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

    def _dry_run_confirmation(self, gap: dict, bet_size: float) -> dict:
        # polymarket_price and kalshi_price are now the actual leg prices (set correctly by Rust)
        poly_price = gap["polymarket_price"]
        kalshi_price = gap["kalshi_price"]
        combined = poly_price + kalshi_price
        k = bet_size / combined if combined > 0 else 0.0
        fee_rate = gap.get("fee_rate", 0.04)
        expected_profit = round(k - bet_size - fee_rate * bet_size, 4)
        mid = gap["market_id"][:8]
        return {
            "polymarket_order_id": f"dry-poly-{mid}",
            "kalshi_order_id": f"dry-kalshi-{mid}",
            "total_spent": round(bet_size, 4),
            "gap_cents": gap.get("gap_cents", 0.0),
            "expected_profit": expected_profit,
            "dry_run": True,
        }

    async def execute(self, gap: dict, bet_size: Optional[float] = None) -> Optional[dict]:
        if bet_size is None:
            bet_size = self._compute_bet_size(gap)

        if self._config.get("dry_run", True):
            return self._dry_run_confirmation(gap, bet_size)

        # Determine order urgency — urgent gaps use aggressive pricing (+0.03 buffer)
        policy = _policy_decide(gap)
        price_buffer = 0.03 if policy.urgency == "high" else 0.0

        pair_type = gap.get("pair_type", "cross_platform")

        # Token side debug log — helps verify polymarket_token is correct before live trading
        log.info(
            "LIVE EXECUTE | market=%s | pair_type=%s | polymarket_token=%s | "
            "kalshi_ticker=%s | poly_price=%.4f | kalshi_price=%.4f | gap=%.1f¢ | bet=$%.2f",
            gap.get("market_id"), pair_type,
            gap.get("polymarket_token"), gap.get("kalshi_ticker"),
            gap.get("polymarket_price", 0), gap.get("kalshi_price", 0),
            gap.get("gap_cents", 0), bet_size,
        )

        # Balance guard — skip if Polymarket wallet can't cover the position
        try:
            balance = await self._poly.get_balance()
            poly_fraction = (1.0 - gap["polymarket_price"]) if pair_type == "cross_platform" else gap["polymarket_price"]
            poly_amount = bet_size * poly_fraction
            if balance < poly_amount:
                log.warning(
                    "Insufficient Polymarket balance: $%.2f available, $%.2f needed — skipping",
                    balance, poly_amount,
                )
                return None
        except Exception as e:
            log.warning("Balance check failed (%s) — proceeding anyway", e)

        if pair_type == "internal":
            return await self._execute_internal(gap, bet_size, price_buffer)
        return await self._execute_cross_platform(gap, bet_size, price_buffer)

    async def _execute_cross_platform(self, gap: dict, bet_size: float, price_buffer: float = 0.0) -> Optional[dict]:
        # polymarket_price and kalshi_price are now the ACTUAL prices of the tokens being bought
        # (set correctly for both directions in Rust comparator — no more manual inversion needed)
        poly_price = gap["polymarket_price"]
        kalshi_price = gap["kalshi_price"]
        combined = poly_price + kalshi_price
        k = bet_size / combined if combined > 0 else 0.0
        poly_amount = round(k * poly_price, 4)
        kalshi_count = max(1, round(k))
        kalshi_action = gap.get("kalshi_action", "buy")
        order_price = min(round(poly_price + price_buffer, 4), 0.99)

        poly_task = self._poly.place_order(
            token_id=gap["polymarket_token"],
            side="BUY",
            amount_usdc=poly_amount,
            price=order_price,
            neg_risk=False,
        )
        kalshi_task = self._kalshi.place_order(
            ticker=gap["kalshi_ticker"],
            action=kalshi_action,
            count=kalshi_count,
        )
        return await self._gather_legs(
            gap, poly_task, kalshi_task, bet_size=bet_size,
            poly_amount=poly_amount, kalshi_count=kalshi_count,
        )

    async def _execute_internal(self, gap: dict, bet_size: float, price_buffer: float = 0.0) -> Optional[dict]:
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
            price=min(round(poly_price + price_buffer, 4), 0.99),
            neg_risk=True,
        )
        task_b = self._poly.place_order(
            token_id=gap["kalshi_ticker"],  # token_b stored in kalshi_ticker for internal pairs
            side="BUY",
            amount_usdc=amount_b,
            price=min(round(kalshi_price + price_buffer, 4), 0.99),
            neg_risk=True,
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

        # Verify fills for legs that returned a response (accepted ≠ filled)
        if a_ok and not self._config.get("dry_run", True):
            poly_id = result_a.get("order_id", "")
            if poly_id:
                filled = await self._wait_for_fill("polymarket", poly_id)
                if not filled:
                    log.warning(
                        "Polymarket order %s did not fill in %ss — canceling",
                        poly_id, _FILL_TIMEOUT,
                    )
                    try:
                        await self._poly.cancel_order(poly_id)
                    except Exception:
                        pass
                    a_ok = False

        if b_ok and kalshi_count is not None and not self._config.get("dry_run", True):
            kalshi_id = result_b.get("order_id", "")
            if kalshi_id:
                filled = await self._wait_for_fill("kalshi", kalshi_id)
                if not filled:
                    log.warning(
                        "Kalshi order %s did not fill in %ss — canceling",
                        kalshi_id, _FILL_TIMEOUT,
                    )
                    try:
                        await self._kalshi.cancel_order(kalshi_id)
                    except Exception:
                        pass
                    b_ok = False

        if a_ok and b_ok:
            fee_rate = gap.get("fee_rate", 0.04)
            combined = gap["polymarket_price"] + gap["kalshi_price"]
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
        pair_type = gap.get("pair_type", "cross_platform")
        try:
            if platform == "polymarket":
                # Sell back at the price we bought — executor adds slippage buffer
                buy_price = filled.get("price", gap.get("polymarket_price", 0.5))
                await self._poly.close_order(
                    token_id=filled["token_id"],
                    amount_usdc=filled["amount_usdc"],
                    price=buy_price,
                    neg_risk=(pair_type == "internal"),
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
