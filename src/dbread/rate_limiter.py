"""Per-connection token-bucket rate limiter (thread-safe, in-memory)."""

from __future__ import annotations

import threading
import time

from .config import Settings


class TokenBucket:
    def __init__(self, rate_per_min: int, burst: int | None = None) -> None:
        self.rate_per_sec = rate_per_min / 60.0
        self.capacity = float(burst if burst is not None else rate_per_min)
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self.last_refill)
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
            self.last_refill = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


class RateLimiter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def acquire(self, connection: str) -> bool:
        bucket = self._buckets.get(connection)
        if bucket is None:
            with self._lock:
                bucket = self._buckets.get(connection)
                if bucket is None:
                    cfg = self.settings.connections.get(connection)
                    if cfg is None:
                        raise KeyError(f"unknown connection: {connection!r}")
                    bucket = TokenBucket(cfg.rate_limit_per_min)
                    self._buckets[connection] = bucket
        return bucket.acquire()
