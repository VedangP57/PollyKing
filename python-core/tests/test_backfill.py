import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "scripts"))

from backfill_matches import compute_resolution_delta_hours, filter_resolution_mismatches


def _make_pair(ticker="KXTEST-25DEC"):
    return {
        "pair_type": "cross_platform",
        "market_id": "test-mkt",
        "token_a": "tok-a",
        "token_b": ticker,
        "kalshi_ticker": ticker,
        "polymarket_slug": "test-slug",
        "confidence": "high",
        "match_method": "fuzzy",
    }


def test_compute_resolution_delta_exact_match():
    kalshi = {"close_time": "2026-06-01T00:00:00Z"}
    poly = {"endDate": "2026-06-01T00:00:00"}
    delta = compute_resolution_delta_hours(kalshi, poly)
    assert delta == 0.0


def test_compute_resolution_delta_5_hour_difference():
    kalshi = {"close_time": "2026-06-01T05:00:00Z"}
    poly = {"endDate": "2026-06-01T00:00:00"}
    delta = compute_resolution_delta_hours(kalshi, poly)
    assert abs(delta - 5.0) < 0.01


def test_compute_resolution_delta_missing_kalshi_date():
    delta = compute_resolution_delta_hours({}, {"endDate": "2026-06-01T00:00:00"})
    assert delta is None


def test_compute_resolution_delta_missing_poly_date():
    delta = compute_resolution_delta_hours({"close_time": "2026-06-01T00:00:00Z"}, {})
    assert delta is None


def test_filter_resolution_mismatches_excludes_large_delta():
    pair = _make_pair("KXTEST-A")
    kalshi_by_ticker = {"KXTEST-A": {"close_time": "2026-06-01T12:00:00Z"}}
    poly_by_token = {"tok-a": {"endDate": "2026-06-01T00:00:00"}}

    kept, mismatches = filter_resolution_mismatches(
        [pair], kalshi_by_ticker, poly_by_token, max_delta_hours=6
    )

    assert len(kept) == 0, "Pair with 12h delta must be excluded"
    assert len(mismatches) == 1
    assert mismatches[0]["delta_hours"] == pytest.approx(12.0, abs=0.01)


def test_filter_resolution_mismatches_downgrades_medium_delta():
    pair = _make_pair("KXTEST-B")
    kalshi_by_ticker = {"KXTEST-B": {"close_time": "2026-06-01T04:00:00Z"}}
    poly_by_token = {"tok-a": {"endDate": "2026-06-01T00:00:00"}}

    kept, mismatches = filter_resolution_mismatches(
        [pair], kalshi_by_ticker, poly_by_token, max_delta_hours=6
    )

    assert len(kept) == 1, "Pair with 4h delta must be kept (under 6h)"
    assert kept[0]["confidence"] == "low", "Confidence must be downgraded to low"
    assert len(mismatches) == 0


def test_filter_resolution_mismatches_passes_zero_delta():
    pair = _make_pair("KXTEST-C")
    kalshi_by_ticker = {"KXTEST-C": {"close_time": "2026-06-01T00:00:00Z"}}
    poly_by_token = {"tok-a": {"endDate": "2026-06-01T00:00:00"}}

    kept, mismatches = filter_resolution_mismatches(
        [pair], kalshi_by_ticker, poly_by_token, max_delta_hours=6
    )

    assert len(kept) == 1
    assert kept[0]["confidence"] == "high", "Exact match must keep original confidence"


def test_filter_resolution_mismatches_unknown_pair_passes():
    pair = _make_pair("KXTEST-UNKNOWN")
    kept, mismatches = filter_resolution_mismatches(
        [pair], {}, {}, max_delta_hours=6
    )
    assert len(kept) == 1, "Pair with missing dates must pass through (no data = no block)"
