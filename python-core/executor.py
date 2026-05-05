import asyncio
import json
from typing import Optional


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

        bet_size = _compute_bet_size(gap_cents, self.config)

        # Equal-payout sizing: buy K contracts of each at their price
        # K = bet_size / combined  →  total spend = K*price_a + K*price_b = bet_size
        # one leg always pays $K, so profit = K*(1 - combined) = bet_size*gap_cents/100
        k = bet_size / combined if combined > 0 else 0.0
        polymarket_amount = k * price_a
        kalshi_amount = k * price_b

        cmd = {
            "action": "execute",
            "pair_type": pair_type,
            "polymarket_side": poly_side,
            "polymarket_amount": round(polymarket_amount, 4),
            "kalshi_side": kalshi_side,
            "kalshi_amount": round(kalshi_amount, 4),
            "gap_cents": round(gap_cents, 4),
            "dry_run": dry_run,
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


def _compute_bet_size(gap_cents: float, config: dict) -> float:
    min_bet = config.get("min_bet_usdc", 10.0)
    max_bet = config.get("max_bet_usdc", 100.0)
    # Scale bet size with gap size — bigger gaps get bigger bets
    scaled = min_bet + (gap_cents / 30.0) * (max_bet - min_bet)
    return min(max(scaled, min_bet), max_bet)
