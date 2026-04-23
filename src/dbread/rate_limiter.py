"""Token-bucket rate limiter: per-connection + optional global (thread-safe)."""

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
        g = getattr(settings, "global_rate_limit_per_min", None)
        self._global: TokenBucket | None = TokenBucket(g) if g else None

    def acquire(self, connection: str) -> bool:
        granted, _ = self.acquire_with_reason(connection)
        return granted

    def acquire_with_reason(self, connection: str) -> tuple[bool, str | None]:
        """Acquire token from global (if set) + per-connection bucket (AND).

        Returns (granted, scope_if_denied). scope is "global" or "connection".
        Global is checked first (cheaper fail-fast; also avoids per-conn
        token debit when global is already exhausted).
        """
        if self._global is not None and not self._global.acquire():
            return False, "global"
        bucket = self._get_bucket(connection)
        if not bucket.acquire():
            return False, "connection"
        return True, None

    def _get_bucket(self, connection: str) -> TokenBucket:
        bucket = self._buckets.get(connection)
        if bucket is not None:
            return bucket
        with self._lock:
            bucket = self._buckets.get(connection)
            if bucket is None:
                cfg = self.settings.connections.get(connection)
                if cfg is None:
                    raise KeyError(f"unknown connection: {connection!r}")
                bucket = TokenBucket(cfg.rate_limit_per_min)
                self._buckets[connection] = bucket
            return bucket
