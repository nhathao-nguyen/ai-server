import threading
import time
from collections import defaultdict, deque

from app.core.errors import ApiError


class RateLimiter:
    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key_id: str, limit: int) -> None:
        now = time.monotonic()
        with self._lock:
            events = self._events[key_id]
            while events and now - events[0] >= self.window_seconds:
                events.popleft()
            if len(events) >= limit:
                raise ApiError(
                    "rate_limit_exceeded",
                    "API key rate limit exceeded",
                    429,
                    {"retry_after_seconds": max(1, int(self.window_seconds - (now - events[0])))},
                )
            events.append(now)
