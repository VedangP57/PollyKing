import logging
from enum import Enum

log = logging.getLogger(__name__)


class KillSwitch(str, Enum):
    DAILY_DRAWDOWN = "daily_drawdown"
    API_HEALTH = "api_health"
    MODEL_DRIFT = "model_drift"
    LIQUIDITY = "liquidity"


_CATEGORY_KEYWORDS = {
    "crypto": ["crypto", "btc", "eth", "sol"],
    "politics": ["politics", "election", "congress", "senate", "president"],
    "sports": ["sports", "nfl", "nba", "mlb", "soccer"],
    "macro": ["fed", "cpi", "gdp", "macro", "rate"],
}


def _infer_category(market_id: str) -> str:
    mid_lower = market_id.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(k in mid_lower for k in keywords):
            return category
    return "other"


class RiskEngine:
    def __init__(self, config: dict, db_conn):
        self.config = config
        self.db = db_conn
        self._switches: dict[str, bool] = {s.value: False for s in KillSwitch}
        self._load_from_db()

    def _load_from_db(self) -> None:
        from tracker import get_bot_state
        for switch in KillSwitch:
            val = get_bot_state(self.db, f"ks_{switch.value}", "false")
            self._switches[switch.value] = val == "true"

    def _persist(self, switch: KillSwitch) -> None:
        from tracker import set_bot_state
        set_bot_state(self.db, f"ks_{switch.value}", str(self._switches[switch.value]).lower())

    def trigger(self, switch: KillSwitch) -> None:
        self._switches[switch.value] = True
        self._persist(switch)
        log.warning(f"Kill switch triggered: {switch.value}")

    def clear(self, switch: KillSwitch) -> None:
        self._switches[switch.value] = False
        self._persist(switch)
        log.info(f"Kill switch cleared: {switch.value}")

    def check_kill_switches(self) -> tuple[bool, str]:
        for switch, active in self._switches.items():
            if active:
                return False, f"Kill switch active: {switch}"
        return True, "ok"

    def check_exposure(self, gap: dict, proposed_amount: float) -> tuple[bool, str]:
        """Check if adding this trade would exceed total portfolio exposure limit."""
        max_cat = self.config.get("max_category_exposure_usdc", 200.0)
        rows = self.db.execute(
            "SELECT COALESCE(SUM(amount_usdc), 0) FROM trades WHERE status='open' AND dry_run=0"
        ).fetchone()
        current_exposure = rows[0] if rows else 0.0
        total_cap = max_cat * 3  # 3x single-category cap = portfolio cap
        if current_exposure + proposed_amount > total_cap:
            return False, f"Exposure {current_exposure + proposed_amount:.0f} > limit {total_cap:.0f}"
        return True, "ok"

    def get_state(self) -> dict:
        return dict(self._switches)
