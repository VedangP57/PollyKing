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


# ── NLP matching (Item 16) ───────────────────────────────────────────────────

from backfill_matches import normalize_title, title_similarity


def test_normalize_removes_punctuation_and_lowercases():
    result = normalize_title("Will the U.S. hit 3% GDP growth in 2026?")
    assert result == result.lower()
    assert "u.s." not in result
    assert "?" not in result


def test_normalize_collapses_whitespace():
    result = normalize_title("  Will  it   rain  ")
    assert "  " not in result
    assert result == result.strip()


def test_normalize_expands_common_abbreviations():
    result = normalize_title("U.K. GDP pct change")
    assert "u.k." not in result
    assert "pct" not in result


def test_title_similarity_identical_titles():
    score = title_similarity("Will BTC reach 100k in 2026", "Will BTC reach 100k in 2026")
    assert score == pytest.approx(1.0)


def test_title_similarity_high_confidence_threshold():
    score = title_similarity(
        "Will Bitcoin exceed 100000 by end of 2026",
        "Will Bitcoin exceed $100000 by end of 2026",
    )
    assert score >= 0.90, f"Expected >= 0.90, got {score:.3f}"


def test_title_similarity_unrelated_titles():
    score = title_similarity(
        "Will the Fed raise rates in 2026",
        "Who will win the 2026 World Cup",
    )
    assert score < 0.80, f"Unrelated titles should score < 0.80, got {score:.3f}"


# ── Pair invalidation (Item 17) ──────────────────────────────────────────────

from backfill_matches import check_pair_active


def test_invalidation_removes_expired_pair():
    pair = _make_pair("KXTEST-EXP")
    poly_status = {"active": False, "end_date_iso": "2025-01-01T00:00:00Z"}
    kalshi_status = {"status": "open"}
    active = check_pair_active(pair, poly_status, kalshi_status)
    assert active is False, "Pair with inactive Polymarket market must be removed"


def test_invalidation_keeps_live_pair():
    pair = _make_pair("KXTEST-LIVE")
    poly_status = {"active": True}
    kalshi_status = {"status": "open"}
    active = check_pair_active(pair, poly_status, kalshi_status)
    assert active is True


def test_invalidation_removes_closed_kalshi():
    pair = _make_pair("KXTEST-CLOSED")
    poly_status = {"active": True}
    kalshi_status = {"status": "closed"}
    active = check_pair_active(pair, poly_status, kalshi_status)
    assert active is False, "Pair with closed Kalshi market must be removed"


def test_invalidation_unknown_status_keeps_pair():
    pair = _make_pair("KXTEST-UNKNOWN")
    active = check_pair_active(pair, None, None)
    assert active is True, "Unknown API status must not invalidate pair (safe default)"
