import time
from collections import deque


class CircuitBreaker:
    def __init__(self, max_failures: int = 3, window_s: float = 600.0):
        self._max_failures = max_failures
        self._window_s = window_s
        self._failures: dict[str, deque] = {}

    def record_failure(self, market_id: str) -> None:
        if market_id not in self._failures:
            self._failures[market_id] = deque()
        self._failures[market_id].append(time.monotonic())

    def is_open(self, market_id: str) -> bool:
        if market_id not in self._failures:
            return False
        now = time.monotonic()
        bucket = self._failures[market_id]
        while bucket and now - bucket[0] >= self._window_s:
            bucket.popleft()
        return len(bucket) >= self._max_failures

    def reset(self, market_id: str) -> None:
        self._failures.pop(market_id, None)
