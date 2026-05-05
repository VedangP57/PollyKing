import asyncio
import json
from typing import Optional

from kelly_engine import compute_arb_kelly_size
from execution_policy import decide as decide_execution


class Executor:
    def __init__(self, config: dict, rust_stdin, rust_stdout_queue: asyncio.Queue):
        self.config = config
        self.rust_stdin = rust_stdin
        self.rust_stdout_queue = rust_stdout_queue
        # Serialize executions: each command→confirmation is a 1-to-1 exchange
        # over the same stdout pipe. Concurrent sends would mix up confirmations.
        self._lock = asyncio.Lock()

    async def execute(self, gap: dict) -> Optional[dict]:
        async with self._lock:
            return await self._execute_locked(gap)

    async def _execute_locked(self, gap: dict) -> Optional[dict]:
        dry_run = self.config.get("dry_run", True)
        pair_type = gap.get("pair_type", "cross_platform")

        if pair_type == "internal":
            # Internal: buy YES on both mutually exclusive Polymarket tokens
            price_a = gap["polymarket_price"]
            price_b = gap["kalshi_price"]  # second Poly token price in internal mode
            combined = price_a + price_b
            gap_cents = (1.0 - combined) * 100.0
            poly_side = "YES"
            kalshi_side = "YES"
        else:
            # Cross-platform: buy NO on Polymarket + buy YES on Kalshi
            price_a = 1.0 - gap["polymarket_price"]   # poly NO price
            price_b = gap["kalshi_price"]              # kalshi YES price
            combined = price_a + price_b
            gap_cents = (1.0 - combined) * 100.0
            poly_side = "NO"
            kalshi_side = "YES"

        if gap_cents <= 0:
            return None

        bet_size = _compute_bet_size(gap, self.config)

        # Equal-payout sizing: buy K contracts of each at their price
        # K = bet_size / combined  →  total spend = K*price_a + K*price_b = bet_size
        # one leg always pays $K, so profit = K*(1 - combined) = bet_size*gap_cents/100
        k = bet_size / combined if combined > 0 else 0.0
        polymarket_amount = k * price_a
        kalshi_amount = k * price_b

        decision = decide_execution(gap)
        cmd = {
            "action": "execute",
            "pair_type": pair_type,
            "polymarket_side": poly_side,
            "polymarket_amount": round(polymarket_amount, 4),
            "kalshi_side": kalshi_side,
            "kalshi_amount": round(kalshi_amount, 4),
            "gap_cents": round(gap_cents, 4),
            "dry_run": dry_run,
            "order_type": decision.order_type,
            "urgency": decision.urgency,
        }

        await self._send_to_rust(cmd)

        # Wait for confirmation from Rust (with timeout)
        try:
            confirmation = await asyncio.wait_for(
                self.rust_stdout_queue.get(), timeout=5.0
            )
            if confirmation.get("event") == "order_placed":
                return confirmation
        except asyncio.TimeoutError:
            pass

        return None

    async def _send_to_rust(self, cmd: dict) -> None:
        line = json.dumps(cmd) + "\n"
        self.rust_stdin.write(line.encode())
        await self.rust_stdin.drain()


def _compute_bet_size(gap: dict, config: dict) -> float:
    """Fractional Kelly bet sizing. Falls back to min_bet if Kelly says NO_BET."""
    bankroll = config.get("bankroll_usdc", 500.0)
    fraction = config.get("kelly_fraction", 0.25)
    min_bet = config.get("min_bet_usdc", 10.0)
    max_bet = config.get("max_bet_usdc", 100.0)
    confidence = gap.get("confidence", "medium")

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
        confidence=confidence,
        fraction=fraction,
        max_bet_pct=0.05,
        min_bet_usdc=min_bet,
        max_bet_usdc=max_bet,
    )
    return result["bet_usdc"] if result["action"] == "BET" else min_bet
