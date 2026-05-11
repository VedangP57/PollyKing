import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Optional
import sqlite3

import tracker


class OpportunityState(Enum):
    DETECTED = auto()
    STABLE = auto()
    EXECUTED = auto()
    COLLAPSED = auto()
    EXPIRED = auto()


@dataclass
class Opportunity:
    opp_key: str
    market_id: str
    pair_type: str
    direction: str
    first_seen: float                    # monotonic
    last_seen: float
    first_gap_cents: float
    max_gap_cents: float
    min_gap_cents: float
    _gap_sum: float = field(default=0.0, repr=False)
    observation_count: int = 0
    state: OpportunityState = OpportunityState.DETECTED
    _gap_history: deque = field(default_factory=lambda: deque(maxlen=20), repr=False)
    poly_bid_size: float = 0.0
    kalshi_ask_size: float = 0.0
    collapse_reason: str = ""
    _last_db_write: float = field(default=0.0, repr=False)

    @property
    def avg_gap_cents(self) -> float:
        return self._gap_sum / self.observation_count if self.observation_count > 0 else 0.0

    @property
    def gap_volatility(self) -> float:
        if len(self._gap_history) < 2:
            return 0.0
        mean = sum(self._gap_history) / len(self._gap_history)
        variance = sum((x - mean) ** 2 for x in self._gap_history) / len(self._gap_history)
        return variance ** 0.5

    @property
    def duration_ms(self) -> int:
        return int((self.last_seen - self.first_seen) * 1000)

    def observe(self, gap_cents: float, poly_bid_size: float = 0.0, kalshi_ask_size: float = 0.0) -> None:
        self.last_seen = time.monotonic()
        self.observation_count += 1
        self._gap_sum += gap_cents
        self.max_gap_cents = max(self.max_gap_cents, gap_cents)
        self.min_gap_cents = min(self.min_gap_cents, gap_cents)
        self._gap_history.append(gap_cents)
        self.poly_bid_size = poly_bid_size
        self.kalshi_ask_size = kalshi_ask_size
        if self.state == OpportunityState.DETECTED and self.observation_count >= 3:
            self.state = OpportunityState.STABLE

    def to_dict(self) -> dict:
        return {
            "opp_key": self.opp_key,
            "market_id": self.market_id,
            "pair_type": self.pair_type,
            "direction": self.direction,
            "first_seen": _mono_to_iso(self.first_seen),
            "last_seen": _mono_to_iso(self.last_seen),
            "first_gap_cents": self.first_gap_cents,
            "max_gap_cents": self.max_gap_cents,
            "min_gap_cents": self.min_gap_cents,
            "avg_gap_cents": round(self.avg_gap_cents, 4),
            "gap_volatility": round(self.gap_volatility, 4),
            "observation_count": self.observation_count,
            "duration_ms": self.duration_ms,
            "state": self.state.name.lower(),
            "poly_bid_size": self.poly_bid_size,
            "kalshi_ask_size": self.kalshi_ask_size,
        }


def _make_opp_key(market_id: str, pair_type: str) -> str:
    return f"{market_id}:{pair_type}"


def _mono_to_iso(mono: float) -> str:
    wall = datetime.now(timezone.utc).timestamp() - (time.monotonic() - mono)
    return datetime.fromtimestamp(wall, tz=timezone.utc).isoformat()


class OpportunityEngine:
    def __init__(
        self,
        db_conn: sqlite3.Connection,
        collapse_threshold_cents: float = 2.0,
        stale_timeout_s: float = 120.0,
        db_write_interval_s: float = 60.0,
    ):
        self._db = db_conn
        self._collapse_threshold = collapse_threshold_cents
        self._stale_timeout = stale_timeout_s
        self._db_write_interval = db_write_interval_s
        self._opps: dict[str, Opportunity] = {}

    def observe(self, gap: dict) -> Optional[Opportunity]:
        market_id = gap["market_id"]
        pair_type = gap.get("pair_type", "cross_platform")
        gap_cents = gap.get("gap_cents", 0.0)
        key = _make_opp_key(market_id, pair_type)

        opp = self._opps.get(key)

        if opp is None or opp.state in (OpportunityState.COLLAPSED, OpportunityState.EXPIRED):
            opp = Opportunity(
                opp_key=key,
                market_id=market_id,
                pair_type=pair_type,
                direction="dir2" if market_id.endswith("-rev") else "dir1",
                first_seen=time.monotonic(),
                last_seen=time.monotonic(),
                first_gap_cents=gap_cents,
                max_gap_cents=gap_cents,
                min_gap_cents=gap_cents,
            )
            self._opps[key] = opp

        opp.observe(
            gap_cents,
            poly_bid_size=gap.get("poly_liquidity_usdc", 0.0),
            kalshi_ask_size=gap.get("kalshi_liquidity_usdc", 0.0),
        )

        if gap_cents < self._collapse_threshold and opp.state in (
            OpportunityState.DETECTED, OpportunityState.STABLE
        ):
            opp.state = OpportunityState.COLLAPSED
            opp.collapse_reason = f"gap {gap_cents:.1f}¢ below threshold {self._collapse_threshold:.1f}¢"

        self._maybe_flush_to_db(opp)
        return opp

    def mark_executed(self, market_id: str, pair_type: str, trade_id: int) -> None:
        key = _make_opp_key(market_id, pair_type)
        opp = self._opps.get(key)
        if opp:
            opp.state = OpportunityState.EXECUTED
            self._flush_to_db(opp)

    def evict_stale(self) -> None:
        now = time.monotonic()
        to_evict = [
            key for key, opp in self._opps.items()
            if (now - opp.last_seen) > self._stale_timeout
            and opp.state not in (OpportunityState.EXECUTED,)
        ]
        for key in to_evict:
            opp = self._opps[key]
            opp.state = OpportunityState.EXPIRED
            self._flush_to_db(opp)
            del self._opps[key]

    def _maybe_flush_to_db(self, opp: Opportunity) -> None:
        now = time.monotonic()
        if (now - opp._last_db_write) >= self._db_write_interval:
            self._flush_to_db(opp)

    def _flush_to_db(self, opp: Opportunity) -> None:
        try:
            tracker.upsert_opportunity(self._db, opp.to_dict())
            opp._last_db_write = time.monotonic()
        except Exception:
            pass  # non-fatal — DB write is analytics, not execution path
