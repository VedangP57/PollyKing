"""Maker/taker routing policy for order execution."""
from dataclasses import dataclass


@dataclass
class ExecutionDecision:
    order_type: str   # "limit" | "market"
    urgency: str      # "high" | "low"
    reason: str


def decide(gap: dict) -> ExecutionDecision:
    """Choose limit vs market order based on gap size, confidence, and time remaining."""
    gap_cents = gap.get("gap_cents", 0.0)
    confidence = gap.get("confidence", "medium")

    # Check time urgency
    closes_at = gap.get("closes_at")
    time_urgent = False
    if closes_at:
        from datetime import datetime, timezone
        try:
            close_dt = datetime.fromisoformat(closes_at.replace("Z", "+00:00"))
            mins_left = (close_dt - datetime.now(timezone.utc)).total_seconds() / 60
            time_urgent = mins_left < 30
        except (ValueError, TypeError):
            pass

    if time_urgent:
        return ExecutionDecision("market", "high", "market closes in < 30 min")

    if gap_cents >= 8.0 and confidence == "high":
        return ExecutionDecision("limit", "low", "large stable gap — prefer spread capture")

    if gap_cents < 3.0 or confidence == "low":
        return ExecutionDecision("market", "high", "small or uncertain gap — fill immediately")

    return ExecutionDecision("limit", "low", "medium gap — default to limit")
