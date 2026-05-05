import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from bayes_engine import BayesEngine


def test_initial_posterior_is_market_price():
    engine = BayesEngine()
    engine.update("mkt-1", new_price=0.40, prev_price=None)
    assert engine.get_posterior("mkt-1") == pytest.approx(0.40, abs=1e-4)


def test_price_increase_raises_posterior():
    engine = BayesEngine()
    engine.update("mkt-1", new_price=0.40, prev_price=None)
    p1 = engine.get_posterior("mkt-1")
    engine.update("mkt-1", new_price=0.45, prev_price=0.40)
    p2 = engine.get_posterior("mkt-1")
    assert p2 > p1


def test_price_decrease_lowers_posterior():
    engine = BayesEngine()
    engine.update("mkt-1", new_price=0.60, prev_price=None)
    p1 = engine.get_posterior("mkt-1")
    engine.update("mkt-1", new_price=0.55, prev_price=0.60)
    p2 = engine.get_posterior("mkt-1")
    assert p2 < p1


def test_posterior_clamped_to_valid_range():
    engine = BayesEngine()
    engine.update("mkt-1", new_price=0.01, prev_price=None)
    for _ in range(20):
        engine.update("mkt-1", new_price=0.99, prev_price=0.01)
    p = engine.get_posterior("mkt-1")
    assert 0.0 < p < 1.0


def test_get_posterior_returns_none_for_unknown_market():
    engine = BayesEngine()
    assert engine.get_posterior("unknown") is None


def test_history_limited_to_100_entries():
    engine = BayesEngine()
    engine.update("mkt-1", 0.5, None)
    for i in range(150):
        engine.update("mkt-1", 0.5 + (i % 3) * 0.01, 0.5)
    assert len(engine.get_history("mkt-1")) <= 100
