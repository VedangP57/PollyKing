import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_rev_gap_gets_fee_rate_from_base_market():
    """A '-rev' gap market_id must look up fee_rate without the suffix."""
    fee_rate_map = {
        "fed-rate-june": 0.02,
        "btc-price-q3": 0.04,
    }

    def lookup_fee(market_id: str, fee_map: dict) -> float:
        lookup_id = market_id.removesuffix("-rev")
        return fee_map.get(lookup_id, fee_map.get(market_id, 0.04))

    assert lookup_fee("fed-rate-june-rev", fee_rate_map) == 0.02
    assert lookup_fee("btc-price-q3", fee_rate_map) == 0.04
    assert lookup_fee("unknown-market", fee_rate_map) == 0.04
    assert lookup_fee("unknown-market-rev", fee_rate_map) == 0.04
