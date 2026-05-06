import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz

FUZZY_THRESHOLD = 85.0


@dataclass
class MarketPair:
    polymarket_slug: str
    kalshi_ticker: str
    market_id: str
    confidence: str
    match_method: str
    pair_type: str = "cross_platform"  # "cross_platform" or "internal"
    token_a: str = ""       # Polymarket YES token hex ID
    no_token_a: str = ""    # Polymarket NO token hex ID (cross-platform dir1: buy Poly NO)
    token_b: str = ""       # Kalshi ticker (cross) or second Poly YES token (internal)
    polymarket_title: str = ""
    kalshi_title: str = ""
    gamma_id_a: str = ""  # Gamma market ID for token_a (used for REST price polling)
    gamma_id_b: str = ""  # Gamma market ID for token_b (internal pairs only)
    outcome_count: int = 0  # Total outcomes in this negRisk event (must be 2 for safe arb)


class Matcher:
    def __init__(self, markets_json_path: str = "config/markets.json"):
        self.manual_pairs: list[dict] = []
        self.last_rejected_multi: int = 0  # set by create_internal_pairs
        self._load_manual_pairs(markets_json_path)

    def _load_manual_pairs(self, path: str) -> None:
        try:
            data = json.loads(Path(path).read_text())
            self.manual_pairs = data.get("manual_pairs", [])
        except (FileNotFoundError, json.JSONDecodeError):
            self.manual_pairs = []

    def match(
        self,
        polymarket_markets: list[dict],
        kalshi_markets: list[dict],
    ) -> list[MarketPair]:
        """Cross-platform matching: Polymarket vs Kalshi."""
        results: list[MarketPair] = []
        matched_kalshi: set[str] = set()

        for poly in polymarket_markets:
            pair = self._find_match(poly, kalshi_markets, matched_kalshi)
            if pair:
                results.append(pair)
                matched_kalshi.add(pair.kalshi_ticker)

        return results

    def create_internal_pairs(
        self,
        polymarket_markets: list[dict],
        full_markets: list[dict] | None = None,
    ) -> list[MarketPair]:
        """Internal matching: group negRisk Polymarket markets by event_id and pair them.

        Only negRisk=True markets are used because negRisk guarantees mutual exclusivity —
        exactly one outcome in the group resolves YES, so combined YES < 1.0 is riskless arb.
        Non-negRisk multi-market events (e.g. 'What will happen before GTA VI?') can have
        multiple YES resolutions, so they would not be guaranteed arb.

        SAFETY: outcome_count is derived from the FULL (unfiltered) market list so that a
        liquidity filter can't make a 10-candidate election look binary. Only groups with
        EXACTLY 2 total outcomes are emitted.

        Args:
            polymarket_markets: Liquid (filtered) markets used for price tracking.
            full_markets: Unfiltered market list for correct outcome counting. Falls back to
                          polymarket_markets if not provided (safe only if already unfiltered).
        """
        # Use the full unfiltered list to count true outcome_count per event
        count_source = full_markets if full_markets is not None else polymarket_markets

        # Build event → total_outcome_count map from the FULL set
        event_outcome_count: dict[str, int] = {}
        for market in count_source:
            if not market.get("negRisk"):
                continue
            events = market.get("events", [])
            if not events:
                continue
            group_key = str(events[0].get("id", ""))
            if not group_key:
                continue
            event_outcome_count[group_key] = event_outcome_count.get(group_key, 0) + 1

        # Group liquid markets by event_id
        groups: dict[str, list[dict]] = {}
        for market in polymarket_markets:
            if not market.get("negRisk"):
                continue
            events = market.get("events", [])
            if not events:
                continue
            group_key = str(events[0].get("id", ""))
            if not group_key:
                continue
            groups.setdefault(group_key, []).append(market)

        pairs: list[MarketPair] = []
        seen: set[tuple] = set()
        rejected_multi = 0

        for group_key, markets in groups.items():
            if len(markets) < 2:
                continue

            # SAFETY CHECK: reject any event that has more than 2 total outcomes
            # (even if only 2 liquid ones are visible after filtering)
            total_outcomes = event_outcome_count.get(group_key, len(markets))
            if total_outcomes != 2:
                rejected_multi += 1
                continue

            for i, m_a in enumerate(markets):
                for m_b in markets[i + 1:]:
                    token_a = _extract_yes_token(m_a)
                    token_b = _extract_yes_token(m_b)
                    if not token_a or not token_b:
                        continue

                    key = (min(token_a, token_b), max(token_a, token_b))
                    if key in seen:
                        continue
                    seen.add(key)

                    title_a = _normalize(m_a.get("question", m_a.get("title", "")))
                    title_b = _normalize(m_b.get("question", m_b.get("title", "")))
                    market_id = f"{group_key}::{token_a[:8]}-{token_b[:8]}"

                    pairs.append(MarketPair(
                        polymarket_slug=m_a.get("slug", ""),
                        kalshi_ticker="",
                        market_id=market_id,
                        confidence="high",
                        match_method="internal",
                        pair_type="internal",
                        token_a=token_a,
                        token_b=token_b,
                        polymarket_title=title_a,
                        kalshi_title=title_b,
                        gamma_id_a=str(m_a.get("id", "")),
                        gamma_id_b=str(m_b.get("id", "")),
                        outcome_count=total_outcomes,
                    ))

        if rejected_multi:
            print(f"  [matcher] Rejected {rejected_multi} event group(s) with >2 outcomes (not safe arb)")

        self.last_rejected_multi = rejected_multi
        return pairs

    def _find_match(
        self,
        poly: dict,
        kalshi_markets: list[dict],
        matched_kalshi: set[str],
    ) -> Optional[MarketPair]:
        poly_slug = poly.get("slug", poly.get("conditionId", ""))
        poly_title = _normalize(poly.get("question", poly.get("title", "")))
        token_a = _extract_yes_token(poly)
        no_token_a = _extract_no_token(poly)

        # Layer 3: manual overrides (checked first for priority)
        for override in self.manual_pairs:
            if override["polymarket_slug"] == poly_slug:
                ticker = override["kalshi_ticker"]
                if ticker not in matched_kalshi:
                    return MarketPair(
                        polymarket_slug=poly_slug,
                        kalshi_ticker=ticker,
                        market_id=poly_slug,
                        confidence=override.get("confidence", "high"),
                        match_method="manual",
                        pair_type="cross_platform",
                        token_a=token_a or poly_slug,
                        no_token_a=no_token_a,
                        token_b=ticker,
                        polymarket_title=poly_title,
                    )

        for kalshi in kalshi_markets:
            ticker = kalshi.get("ticker", "")
            if ticker in matched_kalshi:
                continue

            kalshi_title = _normalize(kalshi.get("title", kalshi.get("subtitle", "")))

            # Layer 1: exact slug/ticker match
            if poly_slug and poly_slug == ticker:
                return MarketPair(
                    polymarket_slug=poly_slug,
                    kalshi_ticker=ticker,
                    market_id=poly_slug,
                    confidence="high",
                    match_method="exact",
                    pair_type="cross_platform",
                    token_a=token_a or poly_slug,
                    no_token_a=no_token_a,
                    token_b=ticker,
                    polymarket_title=poly_title,
                    kalshi_title=kalshi_title,
                )

            # Layer 2: fuzzy title match
            # token_set_ratio handles different-length titles well (e.g. "Will the Fed cut
            # rates in June 2026?" vs "Fed rate cut June meeting") by scoring on the
            # common token subset, ignoring extra words on either side.
            if poly_title and kalshi_title:
                score = fuzz.token_set_ratio(poly_title, kalshi_title)
                if score >= FUZZY_THRESHOLD:
                    confidence = "high" if score >= 95 else "medium"
                    return MarketPair(
                        polymarket_slug=poly_slug,
                        kalshi_ticker=ticker,
                        market_id=poly_slug,
                        confidence=confidence,
                        match_method="fuzzy",
                        pair_type="cross_platform",
                        token_a=token_a or poly_slug,
                        no_token_a=no_token_a,
                        token_b=ticker,
                        polymarket_title=poly_title,
                        kalshi_title=kalshi_title,
                    )

        return None

    def add_manual_pair(
        self,
        polymarket_slug: str,
        kalshi_ticker: str,
        confidence: str = "high",
        notes: str = "",
        markets_json_path: str = "config/markets.json",
    ) -> None:
        entry = {
            "polymarket_slug": polymarket_slug,
            "kalshi_ticker": kalshi_ticker,
            "confidence": confidence,
            "notes": notes,
        }
        self.manual_pairs.append(entry)

        path = Path(markets_json_path)
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            data = {"manual_pairs": []}

        data["manual_pairs"].append(entry)
        path.write_text(json.dumps(data, indent=2))


def _extract_yes_token(market: dict) -> str:
    """Extract the YES outcome token ID from a Gamma API market.

    Gamma API returns clobTokenIds as a JSON-encoded string: '["yes_id", "no_id"]'.
    Index 0 is always the YES token.
    """
    raw = market.get("clobTokenIds", "")
    if raw:
        try:
            ids = json.loads(raw) if isinstance(raw, str) else raw
            if ids:
                return str(ids[0])
        except (json.JSONDecodeError, IndexError):
            pass
    return ""


def _extract_no_token(market: dict) -> str:
    """Extract the NO outcome token ID from a Gamma API market.

    Gamma API returns clobTokenIds as a JSON-encoded string: '["yes_id", "no_id"]'.
    Index 1 is always the NO token.
    """
    raw = market.get("clobTokenIds", "")
    if raw:
        try:
            ids = json.loads(raw) if isinstance(raw, str) else raw
            if len(ids) > 1:
                return str(ids[1])
        except (json.JSONDecodeError, IndexError):
            pass
    return ""


def _normalize(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    # Expand common abbreviations
    replacements = {
        "fed": "federal reserve",
        "bps": "basis points",
        "q1": "first quarter",
        "q2": "second quarter",
        "q3": "third quarter",
        "q4": "fourth quarter",
    }
    for abbr, full in replacements.items():
        text = re.sub(rf"\b{abbr}\b", full, text)
    return text.strip()
