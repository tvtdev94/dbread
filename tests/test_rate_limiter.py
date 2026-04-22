"""Tests for TokenBucket + RateLimiter: burst, refill, concurrency, isolation."""

from __future__ import annotations

import threading

import pytest

from dbread.config import ConnectionConfig, Settings
from dbread.rate_limiter import RateLimiter, TokenBucket


def test_burst_allowed_then_denied() -> None:
    b = TokenBucket(rate_per_min=60, burst=60)
    for _ in range(60):
        assert b.acquire() is True
    assert b.acquire() is False


def test_refill_after_time_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    current = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: current[0])

    b = TokenBucket(rate_per_min=60, burst=60)
    for _ in range(60):
        assert b.acquire() is True
    assert b.acquire() is False

    # advance 2s -> +2 tokens (rate 1/s)
    current[0] += 2.0
    assert b.acquire() is True
    assert b.acquire() is True
    assert b.acquire() is False


def test_full_refill_capped_at_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    current = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: current[0])

    b = TokenBucket(rate_per_min=60, burst=60)
    for _ in range(60):
        b.acquire()
    # wait 1000s -> refill capped at capacity
    current[0] += 1000.0
    granted = sum(1 for _ in range(100) if b.acquire())
    assert granted == 60


def test_low_rate_precision() -> None:
    b = TokenBucket(rate_per_min=6, burst=6)
    granted = sum(1 for _ in range(10) if b.acquire())
    assert granted == 6


def test_thread_safety_no_double_spend() -> None:
    b = TokenBucket(rate_per_min=60, burst=60)
    results: list[bool] = []
    results_lock = threading.Lock()

    def worker() -> None:
        ok = b.acquire()
        with results_lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 60


def _make_settings(**per_conn_rate: int) -> Settings:
    return Settings(
        connections={
            name: ConnectionConfig(
                url=f"sqlite:///:memory:?{name}",
                dialect="sqlite",
                rate_limit_per_min=rate,
            )
            for name, rate in per_conn_rate.items()
        },
    )


def test_rate_limiter_independent_buckets() -> None:
    settings = _make_settings(a=60, b=120)
    rl = RateLimiter(settings)

    for _ in range(60):
        assert rl.acquire("a") is True
    assert rl.acquire("a") is False

    # b still fresh (120 burst available)
    assert rl.acquire("b") is True


def test_rate_limiter_unknown_connection_raises() -> None:
    settings = _make_settings(a=60)
    rl = RateLimiter(settings)
    with pytest.raises(KeyError):
        rl.acquire("nonexistent")


def test_rate_limiter_lazy_bucket_creation() -> None:
    settings = _make_settings(a=30, b=30)
    rl = RateLimiter(settings)
    assert "a" not in rl._buckets
    rl.acquire("a")
    assert "a" in rl._buckets
    assert "b" not in rl._buckets
