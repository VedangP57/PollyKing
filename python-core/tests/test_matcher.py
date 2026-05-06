import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from matcher import Matcher, MarketPair, _normalize


POLY_MARKETS = [
    {"slug": "will-fed-cut-rates-june-2026", "question": "Will the Fed cut rates in June 2026?"},
    {"slug": "btc-above-100k", "question": "Will Bitcoin be above $100k by end of 2026?"},
    {"slug": "exact-slug-match", "question": "Some question"},
    {"slug": "unique-poly-question", "question": "Some totally unique question on polymarket"},
]

KALSHI_MARKETS = [
    {"ticker": "FED-25JUN", "title": "Federal Reserve rate cut June 2026"},
    {"ticker": "BTC-100K", "title": "Bitcoin above 100k by end of 2026"},
    {"ticker": "exact-slug-match", "title": "Some question"},
    {"ticker": "UNRELATED-MARKET", "title": "Something completely unrelated"},
]


def make_matcher(manual_pairs=None) -> Matcher:
    m = Matcher.__new__(Matcher)
    m.manual_pairs = manual_pairs or []
    return m


class TestNormalize:
    def test_lowercases(self):
        assert _normalize("Will FED Cut Rates?") == "will federal reserve cut rates"

    def test_removes_punctuation(self):
        assert "?" not in _normalize("Will it happen?")
        assert "." not in _normalize("By end of 2026.")

    def test_expands_abbreviations(self):
        result = _normalize("Fed rate decision")
        assert "federal reserve" in result

    def test_collapses_whitespace(self):
        result = _normalize("  Will  this   work  ")
        assert "  " not in result


class TestExactMatch:
    def test_exact_slug_match(self):
        matcher = make_matcher()
        pairs = matcher.match(
            [{"slug": "exact-slug-match", "question": "Some question"}],
            [{"ticker": "exact-slug-match", "title": "Some question"}],
        )
        assert len(pairs) == 1
        assert pairs[0].match_method == "exact"
        assert pairs[0].confidence == "high"

    def test_no_duplicate_kalshi_match(self):
        matcher = make_matcher()
        poly = [
            {"slug": "q1", "question": "Question one"},
            {"slug": "q2", "question": "Question one"},
        ]
        kalshi = [{"ticker": "q1", "title": "Question one"}]
        pairs = matcher.match(poly, kalshi)
        assert len(pairs) == 1


class TestFuzzyMatch:
    def test_high_similarity_matches(self):
        matcher = make_matcher()
        pairs = matcher.match(POLY_MARKETS, KALSHI_MARKETS)
        slugs = [p.polymarket_slug for p in pairs]
        assert "btc-above-100k" in slugs

    def test_below_threshold_does_not_match(self):
        matcher = make_matcher()
        poly = [{"slug": "unique-poly-question", "question": "Some totally unique question on polymarket"}]
        kalshi = [{"ticker": "UNRELATED-MARKET", "title": "Something completely unrelated"}]
        pairs = matcher.match(poly, kalshi)
        assert len(pairs) == 0

    def test_confidence_level(self):
        matcher = make_matcher()
        pairs = matcher.match(POLY_MARKETS, KALSHI_MARKETS)
        btc = next((p for p in pairs if p.polymarket_slug == "btc-above-100k"), None)
        assert btc is not None
        assert btc.confidence in ("high", "medium")


class TestManualOverrides:
    def test_manual_override_takes_priority(self):
        manual = [
            {
                "polymarket_slug": "will-fed-cut-rates-june-2026",
                "kalshi_ticker": "FED-MANUAL",
                "confidence": "high",
                "notes": "Manual override test",
            }
        ]
        matcher = make_matcher(manual)
        poly = [{"slug": "will-fed-cut-rates-june-2026", "question": "Will the Fed cut rates in June 2026?"}]
        kalshi = [
            {"ticker": "FED-25JUN", "title": "Federal Reserve rate cut June 2026"},
            {"ticker": "FED-MANUAL", "title": "Fed manual override"},
        ]
        pairs = matcher.match(poly, kalshi)
        assert len(pairs) == 1
        assert pairs[0].kalshi_ticker == "FED-MANUAL"
        assert pairs[0].match_method == "manual"

    def test_manual_confidence_is_preserved(self):
        manual = [
            {
                "polymarket_slug": "will-fed-cut-rates-june-2026",
                "kalshi_ticker": "FED-LOW",
                "confidence": "low",
                "notes": "Low confidence override",
            }
        ]
        matcher = make_matcher(manual)
        poly = [{"slug": "will-fed-cut-rates-june-2026", "question": "Fed rates?"}]
        kalshi = [{"ticker": "FED-LOW", "title": "Fed low"}]
        pairs = matcher.match(poly, kalshi)
        assert pairs[0].confidence == "low"


class TestNoTokenExtraction:
    def test_extract_no_token_returns_second_clob_id(self):
        from matcher import _extract_no_token
        market = {"clobTokenIds": '["yes_abc", "no_xyz"]'}
        assert _extract_no_token(market) == "no_xyz"

    def test_extract_no_token_missing_returns_empty(self):
        from matcher import _extract_no_token
        assert _extract_no_token({}) == ""

    def test_extract_no_token_single_id_returns_empty(self):
        from matcher import _extract_no_token
        market = {"clobTokenIds": '["yes_only"]'}
        assert _extract_no_token(market) == ""

    def test_market_pair_has_no_token_a(self):
        p = MarketPair(
            polymarket_slug="s", kalshi_ticker="k", market_id="m",
            confidence="high", match_method="exact",
            token_a="yes_tok", no_token_a="no_tok", token_b="kal",
        )
        assert p.no_token_a == "no_tok"

    def test_match_populates_no_token_a(self):
        matcher = make_matcher()
        poly = [{"slug": "exact-slug-match", "question": "Some question",
                 "clobTokenIds": '["yes_hex_123", "no_hex_456"]'}]
        kalshi = [{"ticker": "exact-slug-match", "title": "Some question"}]
        pairs = matcher.match(poly, kalshi)
        assert len(pairs) == 1
        assert pairs[0].no_token_a == "no_hex_456"
