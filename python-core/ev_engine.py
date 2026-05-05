from typing import Optional


def calculate_ev(
    market_price: float,
    p_model: float,
    taker_fee_rate: float = 0.02,
) -> dict:
    """EV for a directional binary contract.

    market_price: cost per $1-payout contract (0–1)
    p_model: your estimated win probability
    taker_fee_rate: fee charged on stake (Polymarket default 0.02)
    """
    cost = market_price
    payout_if_win = 1.0 - market_price
    ev = p_model * payout_if_win - (1.0 - p_model) * cost
    fee = taker_fee_rate * cost
    ev_net = ev - fee
    roi = (ev / cost * 100.0) if cost > 0 else 0.0
    return {
        "ev": round(ev, 6),
        "ev_net": round(ev_net, 6),
        "roi": round(roi, 4),
        "verdict": "BUY" if ev_net > 0 else "SKIP",
    }


def calculate_arb_ev(
    combined: float,
    taker_fee_rate: float = 0.02,
    slippage_cents: float = 0.5,
    p_model: Optional[float] = None,
) -> dict:
    """EV for a two-leg arbitrage position.

    combined: sum of both leg prices (< 1.0 → profit opportunity)
    taker_fee_rate: applied to total combined stake
    slippage_cents: expected slippage cost in cents
    p_model: optional Bayesian posterior; scales ev_net by confidence (reserved for Phase 4)
    """
    gap_cents = (1.0 - combined) * 100.0
    fee_cents = taker_fee_rate * combined * 100.0
    ev_net_cents = gap_cents - fee_cents - slippage_cents

    if p_model is not None:
        confidence_factor = abs(p_model - 0.5) * 2.0  # 0=uncertain, 1=certain
        ev_net_cents *= (0.5 + 0.5 * confidence_factor)

    return {
        "ev_cents": round(gap_cents, 4),
        "ev_net_cents": round(ev_net_cents, 4),
        "verdict": "TRADE" if ev_net_cents > 0 else "SKIP",
        "p_model": p_model,
    }
