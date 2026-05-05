import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from kelly_engine import compute_kelly_size, compute_arb_kelly_size


def test_kelly_positive_edge():
    # price=0.30, p_win=0.45 → b=2.333, q=0.55, f*=(2.333*0.45-0.55)/2.333≈0.214
    result = compute_kelly_size(bankroll=1000.0, price=0.30, p_win=0.45)
    assert result["action"] == "BET"
    assert result["f_star"] == pytest.approx(0.2143, abs=1e-3)


def test_kelly_fractional_applied():
    result = compute_kelly_size(bankroll=1000.0, price=0.30, p_win=0.45, fraction=0.25)
    # f = min(0.2143 * 0.25, 0.05) = min(0.0536, 0.05) = 0.05
    assert result["f"] == pytest.approx(0.05, abs=1e-4)
    assert result["bet_usdc"] == pytest.approx(50.0, abs=1e-1)


def test_kelly_negative_edge_returns_no_bet():
    result = compute_kelly_size(bankroll=1000.0, price=0.30, p_win=0.20)
    assert result["action"] == "NO_BET"
    assert result["bet_usdc"] == 0.0


def test_kelly_invalid_price():
    assert compute_kelly_size(1000.0, 0.0, 0.5)["action"] == "NO_BET"
    assert compute_kelly_size(1000.0, 1.0, 0.5)["action"] == "NO_BET"


def test_kelly_respects_max_bet():
    result = compute_kelly_size(
        bankroll=100_000.0, price=0.10, p_win=0.90,
        fraction=0.25, max_bet_usdc=100.0
    )
    assert result["bet_usdc"] <= 100.0


def test_kelly_respects_min_bet():
    result = compute_kelly_size(
        bankroll=10_000.0, price=0.49, p_win=0.52,
        fraction=0.01, min_bet_usdc=10.0
    )
    assert result["bet_usdc"] >= 10.0


def test_arb_kelly_high_confidence():
    # combined=0.90, confidence=high → p_exec=0.92
    # b=(0.10/0.90)=0.1111, q=0.08, f*=(0.1111*0.92-0.08)/0.1111=0.20 → BET
    result = compute_arb_kelly_size(
        bankroll=1000.0, combined=0.90, confidence="high"
    )
    assert result["action"] == "BET"
    assert result["bet_usdc"] > 0


def test_arb_kelly_low_confidence_no_bet():
    # combined=0.98, confidence=low → p_exec=0.75
    # b=(0.02/0.98)=0.0204, f*=(0.0204*0.75-0.25)/0.0204 → very negative → NO_BET
    result = compute_arb_kelly_size(
        bankroll=1000.0, combined=0.98, confidence="low"
    )
    assert result["action"] == "NO_BET"
