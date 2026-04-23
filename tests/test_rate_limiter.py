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


def _make_settings(
    *, global_rate: int | None = None, **per_conn_rate: int
) -> Settings:
    return Settings(
        connections={
            name: ConnectionConfig(
                url=f"sqlite:///:memory:?{name}",
                dialect="sqlite",
                rate_limit_per_min=rate,
            )
            for name, rate in per_conn_rate.items()
        },
        global_rate_limit_per_min=global_rate,
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


def test_global_bucket_blocks_when_exceeded() -> None:
    # Global 5/min, per-conn 100/min → global trips first.
    settings = _make_settings(global_rate=5, a=100)
    rl = RateLimiter(settings)
    for _ in range(5):
        granted, scope = rl.acquire_with_reason("a")
        assert granted is True
        assert scope is None
    granted, scope = rl.acquire_with_reason("a")
    assert granted is False
    assert scope == "global"


def test_per_connection_blocks_when_exceeded_under_global_headroom() -> None:
    # Global 100/min, per-conn 3/min → per-conn trips first.
    settings = _make_settings(global_rate=100, a=3)
    rl = RateLimiter(settings)
    for _ in range(3):
        granted, scope = rl.acquire_with_reason("a")
        assert granted is True
    granted, scope = rl.acquire_with_reason("a")
    assert granted is False
    assert scope == "connection"


def test_no_global_backward_compat() -> None:
    settings = _make_settings(a=2)
    rl = RateLimiter(settings)
    assert rl.acquire("a") is True
    assert rl.acquire("a") is True
    assert rl.acquire("a") is False
    # bool API untouched
    granted, scope = rl.acquire_with_reason("a")
    assert granted is False
    assert scope == "connection"


def test_rotation_attack_blocked_by_global() -> None:
    # Each conn can burst 30/min individually (60 combined would bypass a
    # per-conn-only limit). Global cap 40 → 41st call across ANY conn fails.
    settings = _make_settings(global_rate=40, a=30, b=30)
    rl = RateLimiter(settings)
    granted_total = 0
    denied_on_global = 0
    for i in range(60):
        conn = "a" if i % 2 == 0 else "b"
        granted, scope = rl.acquire_with_reason(conn)
        if granted:
            granted_total += 1
        elif scope == "global":
            denied_on_global += 1
    assert granted_total == 40
    assert denied_on_global > 0


def test_global_rate_validator_rejects_zero() -> None:
    with pytest.raises(Exception):  # noqa: B017  (pydantic ValidationError)
        _make_settings(global_rate=0, a=10)
