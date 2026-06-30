import sys
from pathlib import Path
from unittest.mock import patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import notifier

_GAP = {
    "market_id": "mkt-test",
    "pair_type": "cross_platform",
    "confidence": "high",
    "polymarket_price": 0.55,
    "kalshi_price": 0.45,
    "gap_cents": 10.0,
}


@pytest.fixture(autouse=True)
def clear_rate_limit_state():
    notifier._gap_last.clear()
    notifier._skip_last.clear()
    yield
    notifier._gap_last.clear()
    notifier._skip_last.clear()


def test_gap_detected_calls_metrics():
    with patch.object(notifier._metrics, "inc_gap_detected") as m_inc, \
         patch.object(notifier._metrics, "observe_gap_cents") as m_obs:
        notifier.gap_detected(_GAP)
    m_inc.assert_called_once_with(pair_type="cross_platform", confidence="high")
    m_obs.assert_called_once_with(pair_type="cross_platform", cents=10.0)


def test_gap_detected_rate_limit_suppresses_duplicate():
    with patch.object(notifier._metrics, "inc_gap_detected") as m_inc, \
         patch.object(notifier._metrics, "observe_gap_cents"), \
         patch("notifier.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        notifier.gap_detected(_GAP)
        mock_time.monotonic.return_value = 1005.0  # within 10 s cooldown
        notifier.gap_detected(_GAP)
    assert m_inc.call_count == 1, "Second call within cooldown must be suppressed"


def test_gap_detected_passes_after_cooldown():
    with patch.object(notifier._metrics, "inc_gap_detected") as m_inc, \
         patch.object(notifier._metrics, "observe_gap_cents"), \
         patch("notifier.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        notifier.gap_detected(_GAP)
        mock_time.monotonic.return_value = 1015.0  # beyond 10 s cooldown
        notifier.gap_detected(_GAP)
    assert m_inc.call_count == 2, "Call after cooldown must go through"


def test_gap_rejected_calls_metrics():
    with patch.object(notifier._metrics, "_categorize_rejection", return_value="ev_gate"), \
         patch.object(notifier._metrics, "inc_gap_rejected") as m_rej:
        notifier.gap_rejected("mkt-test", "ev too low: 0.2c")
    m_rej.assert_called_once()


def test_gap_rejected_rate_limit_suppresses_duplicate():
    with patch.object(notifier._metrics, "_categorize_rejection", return_value="ev_gate"), \
         patch.object(notifier._metrics, "inc_gap_rejected") as m_rej, \
         patch("notifier.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        notifier.gap_rejected("mkt-test", "ev too low")
        mock_time.monotonic.return_value = 1030.0  # within 60 s cooldown
        notifier.gap_rejected("mkt-test", "ev too low")
    assert m_rej.call_count == 1, "Same market+reason within cooldown must be suppressed"


def test_gap_rejected_passes_after_cooldown():
    with patch.object(notifier._metrics, "_categorize_rejection", return_value="ev_gate"), \
         patch.object(notifier._metrics, "inc_gap_rejected") as m_rej, \
         patch("notifier.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        notifier.gap_rejected("mkt-test", "ev too low")
        mock_time.monotonic.return_value = 1070.0  # beyond 60 s cooldown
        notifier.gap_rejected("mkt-test", "ev too low")
    assert m_rej.call_count == 2, "Call after cooldown must go through"


def test_different_reasons_tracked_independently():
    with patch.object(notifier._metrics, "_categorize_rejection", return_value="ev_gate"), \
         patch.object(notifier._metrics, "inc_gap_rejected") as m_rej, \
         patch("notifier.time") as mock_time:
        mock_time.monotonic.return_value = 1000.0
        notifier.gap_rejected("mkt-test", "ev too low")
        notifier.gap_rejected("mkt-test", "liquidity insufficient")  # different reason
    assert m_rej.call_count == 2, "Different reasons must each pass through"
