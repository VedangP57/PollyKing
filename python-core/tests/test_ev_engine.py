import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from ev_engine import calculate_ev, calculate_arb_ev


def test_ev_positive_edge():
    result = calculate_ev(market_price=0.12, p_model=0.20)
    assert result["ev"] == pytest.approx(0.08, abs=1e-4)
    assert result["verdict"] == "BUY"


def test_ev_negative_edge():
    result = calculate_ev(market_price=0.12, p_model=0.08)
    assert result["ev"] == pytest.approx(-0.04, abs=1e-4)
    assert result["verdict"] == "SKIP"


def test_ev_net_subtracts_fee():
    result = calculate_ev(market_price=0.50, p_model=0.55, taker_fee_rate=0.02)
    # ev = 0.55*0.50 - 0.45*0.50 = 0.05
    # fee = 0.02 * 0.50 = 0.01
    # ev_net = 0.04
    assert result["ev"] == pytest.approx(0.05, abs=1e-4)
    assert result["ev_net"] == pytest.approx(0.04, abs=1e-4)


def test_arb_ev_positive():
    # combined = 0.92 → gap = 8¢, fee = 0.02*0.92*100=1.84, ev_net = 8-1.84-0.5=5.66
    result = calculate_arb_ev(combined=0.92, taker_fee_rate=0.02, slippage_cents=0.5)
    assert result["ev_cents"] == pytest.approx(8.0, abs=1e-4)
    assert result["ev_net_cents"] == pytest.approx(5.66, abs=1e-2)
    assert result["verdict"] == "TRADE"


def test_arb_ev_negative_after_fees():
    # combined=0.985 → gap=1.5¢, fee=1.97¢, ev_net < 0
    result = calculate_arb_ev(combined=0.985, taker_fee_rate=0.02, slippage_cents=0.5)
    assert result["verdict"] == "SKIP"


def test_arb_ev_no_gap():
    result = calculate_arb_ev(combined=1.0)
    assert result["ev_cents"] == pytest.approx(0.0, abs=1e-4)
    assert result["verdict"] == "SKIP"
