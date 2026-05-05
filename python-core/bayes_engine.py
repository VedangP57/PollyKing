import time
from typing import Optional


class BayesEngine:
    """Per-market Bayesian posterior tracker.

    Prior: market price at first observation.
    Evidence: price delta scaled by sensitivity → likelihood ratio.
    """

    _SENSITIVITY = 4.0
    _LR_CLAMP = (0.1, 10.0)

    def __init__(self):
        self._posteriors: dict[str, float] = {}
        self._history: dict[str, list[tuple[float, float]]] = {}

    def update(self, market_id: str, new_price: float, prev_price: Optional[float]) -> float:
        """Update posterior for market_id given a new observed price. Returns updated posterior."""
        if market_id not in self._posteriors:
            self._posteriors[market_id] = max(0.01, min(0.99, new_price))
            self._history[market_id] = []
            return self._posteriors[market_id]

        prior = self._posteriors[market_id]

        if prev_price is None or prev_price == new_price:
            return prior

        delta = new_price - prev_price
        lr = 1.0 + delta * self._SENSITIVITY
        lr = max(self._LR_CLAMP[0], min(self._LR_CLAMP[1], lr))

        numerator = lr * prior
        posterior = numerator / (numerator + (1.0 - prior))
        posterior = max(0.01, min(0.99, posterior))

        self._posteriors[market_id] = posterior
        history = self._history.setdefault(market_id, [])
        history.append((time.time(), posterior))
        if len(history) > 100:
            self._history[market_id] = history[-100:]

        return posterior

    def get_posterior(self, market_id: str) -> Optional[float]:
        return self._posteriors.get(market_id)

    def get_history(self, market_id: str) -> list[tuple[float, float]]:
        return self._history.get(market_id, [])

    def reset(self, market_id: str) -> None:
        self._posteriors.pop(market_id, None)
        self._history.pop(market_id, None)
