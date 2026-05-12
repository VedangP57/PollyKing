from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

gaps_detected = Counter(
    "arb_gaps_detected_total",
    "Gap detection events emitted by Rust comparator",
    ["pair_type", "confidence"],
)

gaps_rejected = Counter(
    "arb_gaps_rejected_total",
    "Gap events rejected by GapDetector.validate()",
    ["reason_category", "pair_type"],
)

executions = Counter(
    "arb_executions_total",
    "Two-leg execution attempts",
    ["pair_type", "dry_run", "outcome"],
)

fill_polls = Counter(
    "arb_fill_polls_total",
    "Fill polling results after order placement",
    ["platform", "result"],
)

ws_reconnects = Counter(
    "arb_ws_reconnects_total",
    "WebSocket reconnection events",
    ["platform"],
)

emergency_closes = Counter(
    "arb_emergency_closes_total",
    "Emergency close attempts on partial fills",
    ["platform"],
)

# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------

open_positions = Gauge(
    "arb_open_positions_count",
    "Currently open positions",
    ["dry_run"],
)

daily_pnl = Gauge(
    "arb_daily_pnl_usdc",
    "Net P&L for the current trading day (USD)",
)

daily_exposure = Gauge(
    "arb_daily_exposure_usdc",
    "Total notional in open live positions (USD)",
)

active_opportunities = Gauge(
    "arb_active_opportunities_count",
    "Opportunities currently tracked by the lifecycle engine",
    ["state"],
)

ws_staleness = Gauge(
    "arb_ws_staleness_seconds",
    "Seconds since last real price event from Polymarket WS",
)

fill_success_rate = Gauge(
    "arb_fill_success_rate",
    "Fraction of fill polls that returned filled in rolling 1h window",
)

# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------

fill_latency = Histogram(
    "arb_fill_latency_seconds",
    "Time from order placement to fill confirmation",
    ["platform"],
    buckets=[0.5, 1, 2, 5, 10, 20, 30, 60],
)

gap_to_execution = Histogram(
    "arb_gap_to_execution_latency_seconds",
    "Time from gap_detected event to order submission",
    buckets=[0.1, 0.5, 1, 2, 5, 10],
)

opportunity_duration = Histogram(
    "arb_opportunity_duration_seconds",
    "Lifetime of an opportunity from first to last observation",
    ["terminal_state"],
    buckets=[1, 5, 30, 60, 300, 600, 1800, 3600],
)

gap_cents_dist = Histogram(
    "arb_gap_cents",
    "Distribution of gap sizes at detection time",
    ["pair_type"],
    buckets=[5, 8, 10, 12, 15, 20, 25, 30],
)

# ---------------------------------------------------------------------------
# Helper functions — callers use these, never raw prometheus objects
# ---------------------------------------------------------------------------

def inc_gap_detected(pair_type: str = "cross_platform", confidence: str = "medium") -> None:
    gaps_detected.labels(pair_type=pair_type, confidence=confidence).inc()


def inc_gap_rejected(reason: str, pair_type: str = "cross_platform") -> None:
    gaps_rejected.labels(reason_category=reason, pair_type=pair_type).inc()


def inc_execution(pair_type: str, dry_run: bool, outcome: str) -> None:
    executions.labels(pair_type=pair_type, dry_run=str(dry_run), outcome=outcome).inc()


def inc_fill_poll(platform: str, result: str) -> None:
    fill_polls.labels(platform=platform, result=result).inc()


def inc_ws_reconnect(platform: str) -> None:
    ws_reconnects.labels(platform=platform).inc()


def inc_emergency_close(platform: str) -> None:
    emergency_closes.labels(platform=platform).inc()


def observe_fill_latency(platform: str, seconds: float) -> None:
    fill_latency.labels(platform=platform).observe(seconds)


def observe_gap_to_execution(seconds: float) -> None:
    gap_to_execution.observe(seconds)


def observe_opportunity_duration(state: str, seconds: float) -> None:
    opportunity_duration.labels(terminal_state=state).observe(seconds)


def observe_gap_cents(pair_type: str, cents: float) -> None:
    gap_cents_dist.labels(pair_type=pair_type).observe(cents)


def set_open_positions(count: int, dry_run: bool) -> None:
    open_positions.labels(dry_run=str(dry_run)).set(count)


def set_daily_pnl(usdc: float) -> None:
    daily_pnl.set(usdc)


def set_ws_staleness(seconds: float) -> None:
    ws_staleness.set(seconds)


def set_fill_success_rate(rate: float) -> None:
    fill_success_rate.set(rate)


def set_daily_exposure(usdc: float) -> None:
    daily_exposure.set(usdc)


def _categorize_rejection(reason: str) -> str:
    reason_lower = reason.lower()
    if "ev" in reason_lower:
        return "ev_fail"
    if "thin" in reason_lower or "liquidity" in reason_lower:
        return "thin_market"
    if "unstable" in reason_lower:
        return "unstable"
    if "stale" in reason_lower:
        return "stale_feed"
    if "open trade" in reason_lower:
        return "open_trade"
    if "daily loss" in reason_lower:
        return "daily_loss"
    if "position" in reason_lower:
        return "position_limit"
    if "confidence" in reason_lower:
        return "low_confidence"
    if "blacklist" in reason_lower:
        return "blacklisted"
    if "closes in" in reason_lower or "close" in reason_lower:
        return "too_close"
    if "update" in reason_lower or "new" in reason_lower:
        return "too_new"
    return "other"
