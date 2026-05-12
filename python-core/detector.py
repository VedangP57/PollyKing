from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import json

import logging
import sqlite3
from ev_engine import calculate_arb_ev
from risk_engine import RiskEngine
from tracker import get_daily_loss, get_open_position_count, has_open_trade

log = logging.getLogger(__name__)


class GapDetector:
    def __init__(self, config: dict, db_conn: sqlite3.Connection, risk_engine: "RiskEngine | None" = None):
        self.config = config
        self.db_conn = db_conn
        self.risk_engine = risk_engine
        # market_id -> deque of gap_cents (recent history, max 10, O(1) append+pop)
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=10))
        self._stale_flags: dict[str, bool] = {}
        # Load blacklisted event IDs from markets.json
        self._blacklisted_events: set[str] = self._load_blacklist(config.get("markets_json", "config/markets.json"))

    @staticmethod
    def _load_blacklist(markets_json_path: str) -> set[str]:
        try:
            data = json.loads(Path(markets_json_path).read_text())
            return set(str(x) for x in data.get("blacklisted_event_ids", []))
        except (FileNotFoundError, json.JSONDecodeError):
            return set()

    def validate(self, gap: dict) -> tuple[bool, str]:
        market_id = gap["market_id"]
        poly_price = gap["polymarket_price"]
        kalshi_price = gap["kalshi_price"]
        gap_cents = gap["gap_cents"]

        # Check -1: Per-market dedup — reject immediately if we already hold an open
        # position on this market. Prevents double-sizing on persistent gaps.
        if has_open_trade(self.db_conn, market_id):
            return False, f"Already have open trade for {market_id} — skipping"

        # Kill switch gate
        if self.risk_engine:
            ks_ok, ks_reason = self.risk_engine.check_kill_switches()
            if not ks_ok:
                return False, ks_reason

        # Correlated exposure gate
        if self.risk_engine:
            proposed = self.config.get("min_bet_usdc", 10.0)
            exp_ok, exp_reason = self.risk_engine.check_exposure(gap, proposed)
            if not exp_ok:
                return False, exp_reason

        # Check 0a: Blacklisted event gate
        # market_id format for internal pairs: "eventId::tokenA-tokenB"
        event_id = market_id.split("::")[0] if "::" in market_id else ""
        if event_id and event_id in self._blacklisted_events:
            return False, f"Event {event_id} is blacklisted"

        # Check 0b: Binary-only gate — reject multi-outcome markets
        # Only internal negRisk pairs need this check (cross_platform is always binary).
        # outcome_count=0 means unknown — reject by default (safe over sorry).
        pair_type = gap.get("pair_type", "cross_platform")
        if pair_type == "internal":
            # Rust Gap struct emits "polymarket_token" and "kalshi_ticker" (not "token_a"/"token_b")
            # Fall back to token_a/token_b for forward-compat if field names ever change
            token_a = gap.get("polymarket_token", gap.get("token_a", ""))
            token_b = gap.get("kalshi_ticker", gap.get("token_b", ""))
            row = None
            if token_a and token_b:
                row = self.db_conn.execute(
                    "SELECT outcome_count FROM market_pairs WHERE token_a=? AND token_b=?",
                    (token_a, token_b),
                ).fetchone()
            outcome_count = row[0] if row else 0
            if outcome_count == 0:
                return False, "Outcome count unknown — pair not in market_pairs table"
            if outcome_count != 2:
                return False, f"REJECTED: multi-outcome market ({outcome_count} outcomes, need exactly 2)"

        # Check 1: Net EV after fees and slippage must exceed ev_min_cents.
        # Rust sends actual execution prices for both legs (NO price for dir1,
        # YES ask for dir2, YES price for internal). Add directly — no inversion.
        combined = poly_price + kalshi_price

        fee_cache = self.config.get("_fee_cache", {})
        poly_token = gap.get("polymarket_token", "")
        taker_fee_rate = fee_cache.get(poly_token, gap.get("fee_rate", self.config.get("ev_taker_fee_rate", 0.02)))
        slippage_cents = self.config.get("ev_slippage_cents", 0.5)
        ev_min_cents = self.config.get("ev_min_cents", 1.0)

        # Kalshi charges per-contract fees on top of the taker rate.
        # Only applies to cross_platform pairs (internal pairs are both Polymarket, no Kalshi).
        kalshi_fee_cents = 0.0
        if pair_type == "cross_platform":
            bet_usdc = self.config.get("min_bet_usdc", 10.0)
            k_price = gap.get("kalshi_price", 0.5)
            fee_per_contract = self.config.get("kalshi_fee_per_contract", 0.035)
            contracts = max(1, round(bet_usdc / k_price)) if k_price > 0 else 1
            kalshi_fee_cents = (fee_per_contract * contracts / bet_usdc) * 100.0

        ev_result = calculate_arb_ev(
            combined=combined,
            taker_fee_rate=taker_fee_rate,
            slippage_cents=slippage_cents,
            p_model=gap.get("p_model"),
            kalshi_fee_cents=kalshi_fee_cents,
        )
        if ev_result["ev_net_cents"] < ev_min_cents:
            return False, (
                f"EV net {ev_result['ev_net_cents']:.2f}¢ < min {ev_min_cents:.1f}¢ "
                f"(gap {ev_result['ev_cents']:.2f}¢ gross)"
            )

        # Check 1a: Liquidity gate (populated by Rust comparator from order book depth)
        poly_liq = gap.get("poly_liquidity_usdc", float("inf"))
        kalshi_liq = gap.get("kalshi_liquidity_usdc", float("inf"))
        min_liq = self.config.get("min_bet_usdc", 10.0)
        if poly_liq < min_liq or kalshi_liq < min_liq:
            return False, (
                f"Thin market: poly ${poly_liq:.1f} / kalshi ${kalshi_liq:.1f} "
                f"< min ${min_liq:.1f}"
            )

        # Check 1c: Edge-to-spread ratio gate
        # kalshi_spread_cents=0.0 means unknown (internal pairs) — skip gate.
        kalshi_spread_cents = gap.get("kalshi_spread_cents", 0.0)
        min_ratio = self.config.get("min_edge_to_spread_ratio", 3.0)
        if kalshi_spread_cents > 0 and min_ratio > 0:
            edge_to_spread = gap_cents / kalshi_spread_cents
            if edge_to_spread < min_ratio:
                return False, (
                    f"Edge/spread ratio {edge_to_spread:.2f} < {min_ratio:.1f} "
                    f"(gap {gap_cents:.1f}¢ / spread {kalshi_spread_cents:.1f}¢)"
                )

        # Check 1b: Per-pair-type minimum gap threshold
        # Internal pairs have a higher bar (negRisk mechanics more complex)
        if pair_type == "internal":
            min_gap_for_type = self.config.get("internal_min_gap_cents", 8.0)
        else:
            min_gap_for_type = self.config.get("cross_platform_min_gap_cents", 5.0)
        if gap_cents < min_gap_for_type:
            return False, f"Gap {gap_cents:.1f}¢ below {pair_type} minimum {min_gap_for_type:.1f}¢"

        # Check 2: Gap must be stable for 3+ consecutive updates
        history = self._history[market_id]
        history.append(gap_cents)  # deque(maxlen=10) auto-evicts oldest — O(1)

        if len(history) < 3:
            return False, f"Gap too new — only {len(history)} update(s), need 3"

        recent = list(history)[-3:]
        if not _is_stable(recent):
            return False, f"Gap unstable — recent: {recent}"

        # Check 3: Stale feed check
        if self._stale_flags.get(market_id, False):
            return False, "Feed marked stale — skipping"

        # Check 4: Market resolution proximity (requires resolution timestamp in gap)
        closes_at = gap.get("closes_at")
        if closes_at:
            try:
                close_dt = datetime.fromisoformat(closes_at.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                minutes_remaining = (close_dt - now).total_seconds() / 60
                if minutes_remaining < 10:
                    return False, f"Market closes in {minutes_remaining:.1f} min (< 10 min)"
            except (ValueError, TypeError):
                pass

        # Check 5: Daily loss limit
        daily_loss = get_daily_loss(self.db_conn)
        max_loss = self.config.get("max_daily_loss_usdc", 50.0)
        if daily_loss >= max_loss:
            log.critical(
                "DAILY LOSS LIMIT REACHED: $%.2f >= $%.2f — writing kill switch and shutting down",
                daily_loss, max_loss,
            )
            self.db_conn.execute(
                "INSERT OR REPLACE INTO bot_state (key, value) VALUES ('kill_switch', '1')",
            )
            self.db_conn.commit()
            raise SystemExit(1)

        # Check 6: Open position count
        open_positions = get_open_position_count(self.db_conn)
        max_positions = self.config.get("max_open_positions", 5)
        if open_positions >= max_positions:
            return False, f"Max open positions reached: {open_positions}/{max_positions}"

        # Check 7: Confidence level (never auto-execute low confidence)
        confidence = gap.get("confidence", "medium")
        if confidence == "low":
            return False, "Low confidence match — log only, no execution"

        return True, "valid"

    def mark_stale(self, market_id: str) -> None:
        self._stale_flags[market_id] = True

    def clear_stale(self, market_id: str) -> None:
        self._stale_flags[market_id] = False

    def reset_history(self, market_id: str) -> None:
        self._history[market_id] = deque(maxlen=10)


def _is_stable(recent: list[float], tolerance: float = 2.0) -> bool:
    if len(recent) < 2:
        return False
    return max(recent) - min(recent) <= tolerance
