import sys
import time
import sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from tracker import _create_tables
from opportunity_engine import OpportunityEngine, OpportunityState


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_tables(conn)
    return conn


def make_gap(market_id="test-mkt", gap_cents=12.0, pair_type="cross_platform"):
    return {
        "market_id": market_id,
        "pair_type": pair_type,
        "gap_cents": gap_cents,
        "polymarket_price": 0.71,
        "kalshi_price": 0.58,
        "confidence": "high",
    }


def test_first_observation_creates_opportunity():
    db = make_db()
    engine = OpportunityEngine(db)
    opp = engine.observe(make_gap())
    assert opp is not None
    assert opp.state == OpportunityState.DETECTED
    assert opp.observation_count == 1


def test_repeated_observations_update_same_opportunity():
    db = make_db()
    engine = OpportunityEngine(db)
    g = make_gap(gap_cents=12.0)
    for _ in range(5):
        opp = engine.observe(g)
    assert opp.observation_count == 5
    assert len(engine._opps) == 1


def test_stable_after_three_observations():
    db = make_db()
    engine = OpportunityEngine(db)
    g = make_gap()
    for _ in range(3):
        opp = engine.observe(g)
    assert opp.state == OpportunityState.STABLE


def test_collapse_when_gap_drops_below_threshold():
    db = make_db()
    engine = OpportunityEngine(db, collapse_threshold_cents=3.0)
    g_big = make_gap(gap_cents=12.0)
    for _ in range(3):
        engine.observe(g_big)
    g_small = make_gap(gap_cents=1.0)  # below collapse_threshold
    opp = engine.observe(g_small)
    assert opp.state == OpportunityState.COLLAPSED


def test_expired_after_timeout():
    db = make_db()
    engine = OpportunityEngine(db, stale_timeout_s=0.01)
    engine.observe(make_gap())
    time.sleep(0.05)
    engine.evict_stale()
    assert len(engine._opps) == 0  # evicted


def test_different_markets_are_separate_opportunities():
    db = make_db()
    engine = OpportunityEngine(db)
    engine.observe(make_gap(market_id="mkt-a"))
    engine.observe(make_gap(market_id="mkt-b"))
    assert len(engine._opps) == 2


def test_opp_key_uniqueness_by_market_and_direction():
    db = make_db()
    engine = OpportunityEngine(db)
    engine.observe(make_gap(market_id="mkt-x"))
    engine.observe(make_gap(market_id="mkt-x-rev"))  # different direction
    assert len(engine._opps) == 2
