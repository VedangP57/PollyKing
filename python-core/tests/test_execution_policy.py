import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from execution_policy import decide, ExecutionDecision


def _future(minutes: float) -> str:
    dt = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return dt.isoformat()


def test_large_high_confidence_gap_uses_limit():
    gap = {"gap_cents": 10.0, "confidence": "high"}
    decision = decide(gap)
    assert decision.order_type == "limit"
    assert decision.urgency == "low"


def test_small_gap_uses_market_immediately():
    gap = {"gap_cents": 2.0, "confidence": "medium"}
    decision = decide(gap)
    assert decision.order_type == "market"
    assert decision.urgency == "high"


def test_low_confidence_uses_market():
    gap = {"gap_cents": 9.0, "confidence": "low"}
    decision = decide(gap)
    assert decision.order_type == "market"
    assert decision.urgency == "high"


def test_medium_gap_medium_confidence_uses_limit():
    gap = {"gap_cents": 5.0, "confidence": "medium"}
    decision = decide(gap)
    assert decision.order_type == "limit"
    assert decision.urgency == "low"


def test_market_closing_soon_forces_market_order():
    gap = {"gap_cents": 15.0, "confidence": "high", "closes_at": _future(10)}
    decision = decide(gap)
    assert decision.order_type == "market"
    assert decision.urgency == "high"


def test_market_not_closing_soon_uses_normal_logic():
    gap = {"gap_cents": 10.0, "confidence": "high", "closes_at": _future(120)}
    decision = decide(gap)
    assert decision.order_type == "limit"


def test_malformed_closes_at_does_not_crash():
    gap = {"gap_cents": 6.0, "confidence": "medium", "closes_at": "not-a-date"}
    decision = decide(gap)
    assert isinstance(decision, ExecutionDecision)


def test_decision_has_reason_string():
    gap = {"gap_cents": 10.0, "confidence": "high"}
    decision = decide(gap)
    assert isinstance(decision.reason, str) and len(decision.reason) > 0
