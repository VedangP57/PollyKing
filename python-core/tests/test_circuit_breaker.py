import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from circuit_breaker import CircuitBreaker


def test_circuit_breaker_opens_after_max_failures():
    cb = CircuitBreaker(max_failures=3, window_s=600.0)
    cb.record_failure("mkt-A")
    cb.record_failure("mkt-A")
    assert not cb.is_open("mkt-A"), "Should not open after only 2 failures"
    cb.record_failure("mkt-A")
    assert cb.is_open("mkt-A"), "Should open after 3 failures in window"


def test_circuit_breaker_only_affects_named_market():
    cb = CircuitBreaker(max_failures=3, window_s=600.0)
    for _ in range(3):
        cb.record_failure("mkt-A")
    assert not cb.is_open("mkt-B"), "Other markets must not be affected"


def test_circuit_breaker_expires_old_failures():
    cb = CircuitBreaker(max_failures=3, window_s=1.0)
    cb.record_failure("mkt-A")
    cb.record_failure("mkt-A")
    cb.record_failure("mkt-A")
    assert cb.is_open("mkt-A")
    time.sleep(1.1)
    assert not cb.is_open("mkt-A"), "Failures older than window_s should be evicted"


def test_circuit_breaker_resets_on_success():
    cb = CircuitBreaker(max_failures=3, window_s=600.0)
    for _ in range(3):
        cb.record_failure("mkt-A")
    assert cb.is_open("mkt-A")
    cb.reset("mkt-A")
    assert not cb.is_open("mkt-A"), "reset() must clear the breaker"


def test_circuit_breaker_fresh_market_is_closed():
    cb = CircuitBreaker()
    assert not cb.is_open("brand-new-market")
