#!/usr/bin/env python3
"""
Fetches active markets, runs the matcher (mode-aware), writes results to
config/markets.json, and seeds the market_pairs table.

MODE 1 (CROSS PLATFORM): Kalshi markets fetched successfully — match Polymarket vs Kalshi.
                          Kalshi public market data requires NO API key.
MODE 2 (INTERNAL):       Kalshi fetch failed or returned zero markets — fall back to
                          grouping Polymarket markets by event_id (negRisk internal pairs).

KALSHI_API_KEY is only needed for live order placement (DRY_RUN=false).
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).parent.parent  # always the repo root, regardless of CWD
sys.path.insert(0, str(_PROJECT_ROOT / "python-core"))

import tracker
from matcher import Matcher

load_dotenv(_PROJECT_ROOT / ".env")

# Gamma API — fully public, no auth, used for market discovery
POLYMARKET_GAMMA_API = "https://gamma-api.polymarket.com"
# Kalshi public REST API — NO API key required for reading market data/prices
# (key only needed for live order placement when DRY_RUN=false)
KALSHI_API = os.getenv("KALSHI_API_URL", "https://api.elections.kalshi.com/trade-api/v2")
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")  # optional — only for live trading

def _resolve(env_key: str, default: str) -> str:
    """Return absolute path — resolve relative env values against project root."""
    raw = os.getenv(env_key, default)
    p = Path(raw)
    return str(p if p.is_absolute() else _PROJECT_ROOT / p)

MARKETS_JSON = _resolve("MARKETS_JSON", "config/markets.json")
DB_PATH = _resolve("DB_PATH", "data/trades.db")

# Only track markets with at least this much USDC liquidity on each side.
# Filters out illiquid markets where arb would be impossible to fill.
MIN_LIQUIDITY_USDC = float(os.getenv("MIN_LIQUIDITY_USDC", "500"))


async def fetch_polymarket_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Uses Gamma API — fully public, no API key required.

    Fetches liquid markets first by sorting by volume descending.
    Caps at MAX_POLYMARKET_MARKETS to avoid 5-minute pagination runs.
    """
    MAX_POLYMARKET_MARKETS = 5000
    markets = []
    offset = 0
    limit = 500

    print(f"  Fetching Polymarket markets (500/page, max {MAX_POLYMARKET_MARKETS})...", flush=True)
    page = 0
    while len(markets) < MAX_POLYMARKET_MARKETS:
        params = {
            "active": "true",
            "limit": limit,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",
        }
        try:
            async with session.get(f"{POLYMARKET_GAMMA_API}/markets", params=params) as resp:
                if resp.status != 200:
                    print(f"\n  Polymarket Gamma API error: {resp.status}")
                    break
                data = await resp.json()
        except Exception as e:
            print(f"\n  Polymarket request failed: {e}")
            break

        batch = data if isinstance(data, list) else data.get("data", [])
        if not batch:
            break
        markets.extend(batch)
        page += 1
        print(f"  Polymarket: {len(markets)} markets fetched (page {page})...", flush=True)
        if len(batch) < limit:
            break
        offset += limit

    print(f"  Done — {len(markets)} Polymarket markets total")
    return markets


async def fetch_kalshi_markets(session: aiohttp.ClientSession) -> list[dict]:
    """Fetch Kalshi markets via the public REST API — no API key required.

    Uses limit=1000 per page (Kalshi's max) and caps at MAX_KALSHI_MARKETS
    total to prevent hanging on their large market catalog.
    """
    MAX_KALSHI_MARKETS = 5000  # more than enough for matching; avoids infinite pagination
    markets = []
    cursor = None
    page = 0

    # Auth header only needed for live order placement, not for reading
    headers = {"Authorization": f"Token {KALSHI_API_KEY}"} if KALSHI_API_KEY else {}

    print(f"  Fetching Kalshi markets (1000/page, max {MAX_KALSHI_MARKETS})...", flush=True)
    while len(markets) < MAX_KALSHI_MARKETS:
        params = {"status": "open", "limit": 1000}  # max page size = 10x fewer requests
        if cursor:
            params["cursor"] = cursor

        try:
            async with session.get(
                f"{KALSHI_API}/markets", headers=headers, params=params
            ) as resp:
                if resp.status == 401:
                    print(f"\n  Kalshi API 401 — wrong endpoint. Check KALSHI_API_URL in .env")
                    break
                if resp.status != 200:
                    print(f"\n  Kalshi API error: HTTP {resp.status} from {KALSHI_API}")
                    break
                data = await resp.json()
        except Exception as e:
            print(f"\n  Kalshi request failed: {e}")
            break

        batch = data.get("markets", [])
        markets.extend(batch)
        page += 1
        print(f"  Kalshi: {len(markets)} markets fetched (page {page})...", flush=True)

        cursor = data.get("cursor")
        if not cursor or len(batch) == 0:
            break

    print(f"  Done — {len(markets)} Kalshi markets total")
    return markets


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse ISO-8601 string to UTC-aware datetime; returns None on failure."""
    if not s:
        return None
    s = s.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def compute_resolution_delta_hours(
    kalshi_market: dict, poly_market: dict
) -> Optional[float]:
    """Return absolute delta in hours between Kalshi close_time and Polymarket endDate.

    Returns None if either date is missing or unparseable.
    """
    kalshi_dt = _parse_iso(kalshi_market.get("close_time", ""))
    poly_dt = _parse_iso(
        poly_market.get("end_date_iso") or poly_market.get("endDate", "")
    )
    if kalshi_dt is None or poly_dt is None:
        return None
    return abs((kalshi_dt - poly_dt).total_seconds()) / 3600.0


def filter_resolution_mismatches(
    pairs: list[dict],
    kalshi_by_ticker: dict[str, dict],
    poly_by_token: dict[str, dict],
    max_delta_hours: float = 6.0,
) -> tuple[list[dict], list[dict]]:
    """Split pairs into (kept, mismatches).

    - delta > max_delta_hours → excluded (added to mismatches)
    - 0 < delta <= max_delta_hours → kept with confidence downgraded to "low"
    - delta == 0 or dates unavailable → kept unchanged
    """
    kept: list[dict] = []
    mismatches: list[dict] = []

    for pair in pairs:
        ticker = pair.get("kalshi_ticker", "")
        token = pair.get("token_a", "")
        kalshi_mkt = kalshi_by_ticker.get(ticker, {})
        poly_mkt = poly_by_token.get(token, {})

        delta = compute_resolution_delta_hours(kalshi_mkt, poly_mkt)
        if delta is None:
            kept.append(pair)
            continue

        if delta > max_delta_hours:
            mismatches.append({**pair, "delta_hours": delta})
        else:
            entry = {**pair}
            if delta > 0:
                entry["confidence"] = "low"
            kept.append(entry)

    return kept, mismatches


async def main():
    print(f"Backfilling market pairs — fetching Polymarket + Kalshi (public, no key required)...")

    # Honour HTTP_PROXY / HTTPS_PROXY env vars automatically (trust_env=True)
    connector = aiohttp.TCPConnector(ssl=False) if os.getenv("DISABLE_SSL") else None
    async with aiohttp.ClientSession(
        connector=connector,
        trust_env=True,  # picks up HTTP_PROXY / HTTPS_PROXY from environment
        timeout=aiohttp.ClientTimeout(
            total=None,   # no overall cap — pagination can take a while
            connect=10,   # fail fast if host unreachable
            sock_read=15, # fail fast if response stalls mid-stream
        ),
    ) as session:
        poly_markets, kalshi_markets = await asyncio.gather(
            fetch_polymarket_markets(session),
            fetch_kalshi_markets(session),
        )

    # Filter to liquid markets before matching — illiquid markets can't be arbed.
    # IMPORTANT: for internal (negRisk) pairs we pass the FULL unfiltered list as the
    # second argument so outcome_count is computed correctly. Without this, a 10-candidate
    # election where only 2 outcomes have liquidity would appear binary and slip through.
    liquid_markets = [
        m for m in poly_markets
        if float(m.get("liquidityNum") or m.get("liquidity") or 0) >= MIN_LIQUIDITY_USDC
    ]
    print(f"  {len(liquid_markets)} markets with ≥ ${MIN_LIQUIDITY_USDC:.0f} USDC liquidity")

    matcher = Matcher(MARKETS_JSON)

    # Load blacklisted event IDs from markets.json
    try:
        _cfg = json.loads(Path(MARKETS_JSON).read_text())
        blacklisted_events = set(str(x) for x in _cfg.get("blacklisted_event_ids", []))
    except (FileNotFoundError, json.JSONDecodeError):
        blacklisted_events = set()

    if blacklisted_events:
        before = len(liquid_markets)
        liquid_markets = [
            m for m in liquid_markets
            if str((m.get("events") or [{}])[0].get("id", "")) not in blacklisted_events
        ]
        poly_markets = [
            m for m in poly_markets
            if str((m.get("events") or [{}])[0].get("id", "")) not in blacklisted_events
        ]
        print(f"  Blacklisted events removed: {before - len(liquid_markets)} liquid markets filtered")

    # Mode is determined by whether Kalshi actually returned markets — no key needed
    kalshi_mode = len(kalshi_markets) > 0
    mode_label = "CROSS PLATFORM (Polymarket ↔ Kalshi)" if kalshi_mode else "INTERNAL (Polymarket negRisk)"
    print(f"Mode: {mode_label}")

    # Build fee_rate lookup: gamma_id → fee_rate
    # feeSchedule.rate is the taker fee (0.04 = 4% for politics, 0.02 = 2% for others)
    fee_rate_by_gamma_id: dict[str, float] = {}
    for m in poly_markets:
        gamma_id = str(m.get("id", ""))
        schedule = m.get("feeSchedule") or {}
        rate = float(schedule.get("rate", 0.04))
        if gamma_id:
            fee_rate_by_gamma_id[gamma_id] = rate

    if kalshi_mode:
        cross_pairs = matcher.match(liquid_markets, kalshi_markets)
        internal_pairs = matcher.create_internal_pairs(liquid_markets, full_markets=poly_markets)

        # Resolution mismatch check — filter cross-platform pairs only
        max_delta_h = float(os.getenv("MAX_RESOLUTION_DELTA_HOURS", "6"))
        kalshi_by_ticker = {m.get("ticker", ""): m for m in kalshi_markets if m.get("ticker")}
        # Build token_a → poly market lookup via clobTokenIds
        poly_by_token: dict[str, dict] = {}
        for m in liquid_markets:
            for token in m.get("clobTokenIds") or []:
                poly_by_token[token] = m
        cross_pair_dicts = [
            {"pair_type": p.pair_type, "market_id": p.market_id,
             "token_a": p.token_a, "kalshi_ticker": p.kalshi_ticker,
             "confidence": p.confidence}
            for p in cross_pairs
        ]
        kept_dicts, mismatch_dicts = filter_resolution_mismatches(
            cross_pair_dicts, kalshi_by_ticker, poly_by_token, max_delta_hours=max_delta_h
        )
        if mismatch_dicts:
            mismatch_path = Path(_resolve("DB_PATH", "data/trades.db")).parent / "resolution_mismatches.json"
            mismatch_path.write_text(json.dumps(mismatch_dicts, indent=2))
            print(f"  WARNING: {len(mismatch_dicts)} cross-platform pairs excluded due to resolution mismatch > {max_delta_h}h — see {mismatch_path}")
        kept_market_ids = {d["market_id"] for d in kept_dicts}
        cross_pairs = [p for p in cross_pairs if p.market_id in kept_market_ids]
        # Apply confidence downgrades
        conf_override = {d["market_id"]: d["confidence"] for d in kept_dicts}
        for p in cross_pairs:
            if conf_override.get(p.market_id) == "low":
                p.confidence = "low"

        pairs = cross_pairs + internal_pairs
        print(f"  Cross-platform: {len(cross_pairs)} pairs | Internal fallback: {len(internal_pairs)} pairs")
    else:
        print("  Kalshi returned 0 markets — falling back to internal negRisk pairs only")
        pairs = matcher.create_internal_pairs(liquid_markets, full_markets=poly_markets)

    print(f"Matched {len(pairs)} pairs")

    # Build new "pairs" list for markets.json
    pairs_entries = []
    for pair in pairs:
        entry = {
            "pair_type": pair.pair_type,
            "token_a": pair.token_a,
            "no_token_a": pair.no_token_a,
            "token_b": pair.token_b,
            "market_id": pair.market_id,
            "confidence": pair.confidence,
            "match_method": pair.match_method,
            "gamma_id_a": pair.gamma_id_a,
            "gamma_id_b": pair.gamma_id_b,
            "outcome_count": pair.outcome_count,
            "fee_rate": fee_rate_by_gamma_id.get(pair.gamma_id_a, 0.04),
        }
        if pair.pair_type == "cross_platform":
            entry["polymarket_slug"] = pair.polymarket_slug
            entry["kalshi_ticker"] = pair.kalshi_ticker
        pairs_entries.append(entry)

    # Bail out without touching markets.json if both fetches failed — API outage
    # should not wipe out a previously-good pair list.
    if not poly_markets and not kalshi_markets:
        print("ERROR: Both Polymarket and Kalshi returned 0 markets — API outage? Keeping existing markets.json.")
        return

    # Load existing file to preserve manual_pairs
    markets_path = Path(MARKETS_JSON)
    try:
        existing = json.loads(markets_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        existing = {"pairs": [], "manual_pairs": []}

    existing["pairs"] = pairs_entries
    existing["_stats"] = {
        "rejected_multi_outcome": matcher.last_rejected_multi,
        "total_pairs": len(pairs_entries),
    }
    markets_path.write_text(json.dumps(existing, indent=2))
    print(f"Wrote {len(pairs_entries)} pairs to {MARKETS_JSON}")

    # Seed market_pairs table
    db_conn = tracker.init_db(DB_PATH)
    for pair in pairs:
        tracker.log_market_pair(db_conn, {
            "pair_type": pair.pair_type,
            "token_a": pair.token_a,
            "token_b": pair.token_b,
            "polymarket_slug": pair.polymarket_slug,
            "kalshi_ticker": pair.kalshi_ticker,
            "confidence": pair.confidence,
            "match_method": pair.match_method,
            "gamma_id_a": pair.gamma_id_a,
            "gamma_id_b": pair.gamma_id_b,
            "outcome_count": pair.outcome_count,
        })
    db_conn.close()
    print(f"Seeded {len(pairs)} pairs into {DB_PATH}")

    high = sum(1 for p in pairs if p.confidence == "high")
    medium = sum(1 for p in pairs if p.confidence == "medium")
    low = sum(1 for p in pairs if p.confidence == "low")
    print(f"\nSummary: {high} high confidence | {medium} medium | {low} low")
    print("\nDone. Run: python python-core/main.py")


if __name__ == "__main__":
    asyncio.run(main())
