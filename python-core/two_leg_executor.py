import asyncio
import logging
import sqlite3
import time as _time
from datetime import datetime, timezone
from typing import Optional

import metrics as _metrics
from execution_policy import decide as _policy_decide
from kalshi_executor import ExecutorError, KalshiExecutor
from kelly_engine import compute_arb_kelly_size
from polymarket_executor import PolymarketExecutor
from tracker import log_emergency_position, log_execution_event, update_execution_fill

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
                # Polymarket uses "matched"; Kalshi uses "executed"
                if status in ("matched", "executed"):
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

        # Capture signal prices at decision time — before any exchange latency
        _signal_at = datetime.now(timezone.utc).isoformat()
        _signal_snap = {
            "market_id": gap.get("market_id", ""),
            "pair_type": gap.get("pair_type", "cross_platform"),
            "opp_id": gap.get("opp_id"),
            "signal_poly_price": gap.get("polymarket_price"),
            "signal_kalshi_price": gap.get("kalshi_price"),
            "signal_gap_cents": gap.get("gap_cents"),
            "signal_poly_liquidity": gap.get("poly_liquidity_usdc"),
            "signal_kalshi_liquidity": gap.get("kalshi_liquidity_usdc"),
            "signal_at": _signal_at,
        }

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
            result = await self._execute_internal(gap, bet_size, price_buffer)
        else:
            result = await self._execute_cross_platform(gap, bet_size, price_buffer)

        if result is not None:
            _submitted_at = datetime.now(timezone.utc).isoformat()
            exec_evt = {
                **_signal_snap,
                "submitted_poly_price": gap.get("polymarket_price"),
                "submitted_kalshi_price": gap.get("kalshi_price"),
                "price_buffer_applied": price_buffer,
                "submitted_at": _submitted_at,
                "urgency": policy.urgency,
            }
            try:
                log_execution_event(self._db, exec_evt)
            except Exception:
                pass  # telemetry is non-fatal

        return result

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
            poly_liquidity_usdc=gap.get("poly_liquidity_usdc", float("inf")),
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
            poly_amount=amount_a, kalshi_count=None, kalshi_amount=amount_b,
        )

    async def _poll_and_cancel(self, platform: str, order_id: str) -> bool:
        t0 = _time.monotonic()
        filled = await self._wait_for_fill(platform, order_id)
        elapsed = _time.monotonic() - t0
        if filled:
            _metrics.observe_fill_latency(platform, elapsed)
            _metrics.inc_fill_poll(platform, "filled")
        else:
            _metrics.inc_fill_poll(platform, "timeout")
            log.warning(
                "%s order %s did not fill in %ss — canceling",
                platform, order_id, _FILL_TIMEOUT,
            )
            try:
                if platform == "polymarket":
                    await self._poly.cancel_order(order_id)
                elif platform == "kalshi":
                    await self._kalshi.cancel_order(order_id)
            except Exception:
                pass
            return False
        return True

    async def _gather_legs(
        self,
        gap: dict,
        task_a,
        task_b,
        bet_size: float,
        poly_amount: float,
        kalshi_count: Optional[int],
        kalshi_amount: float = 0.0,
    ) -> Optional[dict]:
        result_a, result_b = await asyncio.gather(task_a, task_b, return_exceptions=True)

        a_ok = not isinstance(result_a, Exception)
        b_ok = not isinstance(result_b, Exception)

        # Verify fills concurrently — both polls run in parallel so max wait is
        # 1×_FILL_TIMEOUT instead of 2×_FILL_TIMEOUT for sequential polling.
        dry_run = self._config.get("dry_run", True)
        verify: list[tuple[str, object]] = []  # (label, coroutine)

        if a_ok and not dry_run:
            poly_id = result_a.get("order_id", "")
            if poly_id:
                verify.append(("poly", self._poll_and_cancel("polymarket", poly_id)))

        if b_ok and not dry_run:
            b_id = result_b.get("order_id", "")
            if b_id:
                b_platform = "kalshi" if kalshi_count is not None else "polymarket"
                verify.append((b_platform, self._poll_and_cancel(b_platform, b_id)))

        if verify:
            labels, coros = zip(*verify)
            outcomes = await asyncio.gather(*coros)
            for label, ok in zip(labels, outcomes):
                if not ok:
                    if label == "poly":
                        a_ok = False
                    else:
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

        # Partial fill — emergency close the filled leg using explicit routing,
        # not filled.get("platform") which is absent from real exchange responses.
        pair_type = gap.get("pair_type", "cross_platform")
        if a_ok:
            filled_order_id = result_a.get("order_id", "")
            log.error(
                "PARTIAL FILL on %s — poly filled %s, leg_b failed: %s — emergency closing poly",
                gap["market_id"], filled_order_id, result_b,
            )
            await self._emergency_close(
                platform="polymarket",
                order_id=filled_order_id,
                token_or_ticker=gap.get("polymarket_token", ""),
                amount_usdc=poly_amount,
                count=None,
                price=gap["polymarket_price"],
                pair_type=pair_type,
                market_id=gap["market_id"],
            )
        else:
            filled_order_id = result_b.get("order_id", "")
            log.error(
                "PARTIAL FILL on %s — leg_b filled %s, poly failed: %s — emergency closing leg_b",
                gap["market_id"], filled_order_id, result_a,
            )
            if pair_type == "cross_platform":
                b_platform = "kalshi"
                b_ticker = gap.get("kalshi_ticker", "")
                b_amount = kalshi_amount if kalshi_amount > 0 else round(
                    (kalshi_count or 1) * gap["kalshi_price"], 4
                )
            else:  # internal — both legs are Polymarket; kalshi_ticker holds token_b
                b_platform = "polymarket"
                b_ticker = gap.get("kalshi_ticker", "")
                b_amount = kalshi_amount
            await self._emergency_close(
                platform=b_platform,
                order_id=filled_order_id,
                token_or_ticker=b_ticker,
                amount_usdc=b_amount,
                count=kalshi_count,
                price=gap["kalshi_price"],
                pair_type=pair_type,
                market_id=gap["market_id"],
            )
        return None

    async def _emergency_close(
        self,
        platform: str,
        order_id: str,
        token_or_ticker: str,
        amount_usdc: float,
        count: Optional[int],
        price: float,
        pair_type: str,
        market_id: str,
    ) -> None:
        try:
            if platform == "polymarket":
                await self._poly.close_order(
                    token_id=token_or_ticker,
                    amount_usdc=amount_usdc,
                    price=price,
                    neg_risk=(pair_type == "internal"),
                )
            elif platform == "kalshi":
                await self._kalshi.close_order(
                    ticker=token_or_ticker,
                    count=count,
                )
            status = "closed_auto"
        except Exception as e:
            log.error(
                "Emergency close FAILED for %s on %s: %s — REQUIRES MANUAL ACTION",
                order_id, platform, e,
            )
            status = "open"

        _metrics.inc_emergency_close(platform)
        log_emergency_position(self._db, {
            "market_id": market_id,
            "platform": platform,
            "order_id": order_id,
            "side": token_or_ticker,
            "amount_usdc": amount_usdc,
        })
        if status == "closed_auto":
            self._db.execute(
                "UPDATE emergency_positions SET status='closed_auto', closed_at=datetime('now') "
                "WHERE order_id=?",
                (order_id,),
            )
            self._db.commit()
