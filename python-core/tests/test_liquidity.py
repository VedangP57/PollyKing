import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from liquidity import estimate_slippage_cents


def test_zero_depth_returns_max_slippage():
    result = estimate_slippage_cents(bet_usdc=50.0, top_of_book_usdc=0.0)
    assert result == pytest.approx(0.3 + 5.0)


def test_tiny_bet_vs_large_book_is_near_base():
    # bet_usdc=1, book=10000 → depth_ratio ≈ 0.0001 → impact ≈ 0
    result = estimate_slippage_cents(bet_usdc=1.0, top_of_book_usdc=10_000.0)
    assert result < 0.35, f"Tiny bet should have near-zero market impact, got {result}"
    assert result >= 0.3, "Should always include base_slippage"


def test_full_book_consumption_returns_max():
    # depth_ratio = 1.0 → result = base + impact_factor = 0.3 + 5.0
    result = estimate_slippage_cents(bet_usdc=500.0, top_of_book_usdc=500.0)
    assert result == pytest.approx(0.3 + 5.0)


def test_half_book_consumption_is_between_base_and_max():
    result = estimate_slippage_cents(bet_usdc=50.0, top_of_book_usdc=100.0)
    assert 0.3 < result < 5.3, f"Half-book consumption should be between base and max, got {result}"


def test_larger_bet_produces_more_slippage():
    small = estimate_slippage_cents(bet_usdc=10.0, top_of_book_usdc=100.0)
    large = estimate_slippage_cents(bet_usdc=80.0, top_of_book_usdc=100.0)
    assert large > small, "Larger position should produce more slippage"


def test_custom_params_respected():
    result = estimate_slippage_cents(
        bet_usdc=50.0, top_of_book_usdc=100.0,
        base_slippage=0.0, impact_factor=10.0,
    )
    assert result > 0.0
    assert result <= 10.0
