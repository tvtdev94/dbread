"""Micro-benchmark: measure sqlglot parse, LIMIT injection, rate-limit acquire.

Usage:
    uv run python scripts/benchmark_overhead.py [--iters 10000]

Numbers are *your hardware*; paste into docs/benchmarks.md or README
overhead table for transparency.
"""

from __future__ import annotations

import argparse
import statistics
import time
from collections.abc import Callable

from dbread.config import ConnectionConfig, Settings
from dbread.rate_limiter import RateLimiter
from dbread.sql_guard import SqlGuard

WORKLOADS: dict[str, str] = {
    "trivial": "SELECT 1",
    "realistic": (
        "SELECT id, name, email, status FROM users "
        "WHERE status = 'active' AND created_at > '2024-01-01' "
        "ORDER BY created_at DESC"
    ),
    "complex": (
        "WITH t1 AS (SELECT id FROM a JOIN b ON a.id=b.a_id), "
        "t2 AS (SELECT id FROM c JOIN d ON c.id=d.c_id), "
        "t3 AS (SELECT id FROM e JOIN f ON e.id=f.e_id), "
        "t4 AS (SELECT id FROM g JOIN h ON g.id=h.g_id), "
        "t5 AS (SELECT id FROM i JOIN j ON i.id=j.i_id) "
        "SELECT t1.id FROM t1, t2, t3, t4, t5 "
        "WHERE t1.id=t2.id AND t2.id=t3.id AND t3.id=t4.id AND t4.id=t5.id "
        "ORDER BY t1.id"
    ),
}


def _time_ns(fn: Callable[[], None], iters: int) -> list[int]:
    # Warmup so JIT / lazy-imports don't skew first runs.
    for _ in range(100):
        fn()
    samples = [0] * iters
    for i in range(iters):
        t0 = time.perf_counter_ns()
        fn()
        samples[i] = time.perf_counter_ns() - t0
    return samples


def _percentiles(samples: list[int]) -> dict[str, float]:
    s = sorted(samples)
    n = len(s)
    return {
        "p50": s[n // 2] / 1000,
        "p95": s[int(n * 0.95)] / 1000,
        "p99": s[int(n * 0.99)] / 1000,
        "mean": statistics.mean(s) / 1000,
        "stdev": statistics.pstdev(s) / 1000,
    }


def _settings() -> Settings:
    return Settings(
        connections={
            "bench": ConnectionConfig(
                url="sqlite:///:memory:",
                dialect="postgres",
                rate_limit_per_min=100_000,
            ),
        },
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--iters", type=int, default=10_000)
    args = p.parse_args()

    guard = SqlGuard()
    rl = RateLimiter(_settings())

    print(f"# dbread overhead — {args.iters} iters (+ 100 warmup); times in microseconds")
    print()
    hdr = f"{'workload':<12} {'step':<18} {'p50':>9} {'p95':>9} {'p99':>9} {'mean':>9} {'stdev':>9}"
    print(hdr)
    print("-" * len(hdr))

    def print_row(wl: str, step: str, samples: list[int]) -> None:
        pct = _percentiles(samples)
        print(
            f"{wl:<12} {step:<18} "
            f"{pct['p50']:>9.2f} {pct['p95']:>9.2f} {pct['p99']:>9.2f} "
            f"{pct['mean']:>9.2f} {pct['stdev']:>9.2f}"
        )

    for wl_name, sql in WORKLOADS.items():
        print_row(
            wl_name, "guard.validate",
            _time_ns(lambda: guard.validate(sql, "postgres"), args.iters),
        )
        print_row(
            wl_name, "guard.inject_limit",
            _time_ns(lambda: guard.inject_limit(sql, "postgres", 1000), args.iters),
        )

    print_row(
        "(shared)", "rate_limit.acquire",
        _time_ns(lambda: rl.acquire("bench"), args.iters),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
