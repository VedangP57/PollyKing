import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_metrics_module_importable():
    import metrics
    assert hasattr(metrics, "gaps_detected")
    assert hasattr(metrics, "gaps_rejected")
    assert hasattr(metrics, "executions")
    assert hasattr(metrics, "fill_latency")
    assert hasattr(metrics, "open_positions")
    assert hasattr(metrics, "daily_pnl")
    assert hasattr(metrics, "ws_reconnects")
    assert hasattr(metrics, "inc_gap_detected")
    assert hasattr(metrics, "inc_gap_rejected")
    assert hasattr(metrics, "inc_execution")
    assert hasattr(metrics, "observe_fill_latency")
    assert hasattr(metrics, "set_open_positions")
    assert hasattr(metrics, "set_daily_pnl")


def test_inc_gap_detected_increments_counter():
    import metrics
    from prometheus_client import REGISTRY
    before = REGISTRY.get_sample_value(
        "arb_gaps_detected_total",
        {"pair_type": "cross_platform", "confidence": "high"}
    ) or 0.0
    metrics.inc_gap_detected(pair_type="cross_platform", confidence="high")
    after = REGISTRY.get_sample_value(
        "arb_gaps_detected_total",
        {"pair_type": "cross_platform", "confidence": "high"}
    )
    assert after == before + 1.0


def test_inc_gap_rejected_with_reason():
    import metrics
    from prometheus_client import REGISTRY
    before = REGISTRY.get_sample_value(
        "arb_gaps_rejected_total",
        {"reason_category": "ev_fail", "pair_type": "cross_platform"}
    ) or 0.0
    metrics.inc_gap_rejected(reason="ev_fail", pair_type="cross_platform")
    after = REGISTRY.get_sample_value(
        "arb_gaps_rejected_total",
        {"reason_category": "ev_fail", "pair_type": "cross_platform"}
    )
    assert after == before + 1.0
