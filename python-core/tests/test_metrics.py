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


import pytest

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


def test_set_ws_staleness_updates_gauge():
    import metrics
    from prometheus_client import REGISTRY
    metrics.set_ws_staleness(42.5)
    val = REGISTRY.get_sample_value("arb_ws_staleness_seconds")
    assert val == pytest.approx(42.5)


def test_set_fill_success_rate_updates_gauge():
    import metrics
    from prometheus_client import REGISTRY
    metrics.set_fill_success_rate(0.87)
    val = REGISTRY.get_sample_value("arb_fill_success_rate")
    assert val == pytest.approx(0.87)


def test_pnl_metric_updates():
    import metrics
    from prometheus_client import REGISTRY
    metrics.set_daily_pnl(12.34)
    val = REGISTRY.get_sample_value("arb_daily_pnl_usdc")
    assert val == pytest.approx(12.34)
