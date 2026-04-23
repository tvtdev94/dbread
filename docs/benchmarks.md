# dbread — Overhead Benchmark

## TL;DR

dbread sits between your AI and the DB. The AI sees this overhead **per query**, on top of whatever the DB itself spends. These numbers are *per-step*, in microseconds, measured on the machine below.

| Workload | `guard.validate` p95 | `guard.inject_limit` p95 | Total guard overhead p95 |
|----------|---------------------:|--------------------------:|--------------------------:|
| `SELECT 1` (trivial) | ~0.17 ms | ~0.44 ms | **~0.6 ms** |
| `SELECT ... WHERE ... ORDER BY` (realistic) | ~0.65 ms | ~1.28 ms | **~1.9 ms** |
| 5-CTE 10-table join (complex) | ~3.1 ms | ~4.8 ms | **~7.9 ms** |

Rate-limit acquire: **~1.3 µs p95** — effectively free.

## Methodology

- Script: [`scripts/benchmark_overhead.py`](../scripts/benchmark_overhead.py).
- Timer: `time.perf_counter_ns`, 5 000 iterations per workload, 100-iteration warmup discarded.
- Measures only in-process work: sqlglot parse + AST walk (`validate`), AST rewrite + re-serialise (`inject_limit`), token-bucket `acquire`.
- **Not measured:** DB round-trip, network, serialisation of the MCP response. Those dominate real latency.
- Run it yourself:

```bash
uv run python scripts/benchmark_overhead.py --iters 10000
```

## Raw numbers (sample run — Python 3.12, Windows 11)

```
workload     step                     p50       p95       p99      mean     stdev
---------------------------------------------------------------------------------
trivial      guard.validate         98.20    166.10    235.40    111.26    176.06
trivial      guard.inject_limit    268.90    436.70    572.00    297.83     75.34
realistic    guard.validate        435.10    650.40    831.30    473.08     93.15
realistic    guard.inject_limit    788.40   1278.70   1706.80    865.00    333.62
complex      guard.validate       1938.40   3105.90   4022.80   2129.98    925.29
complex      guard.inject_limit   3175.40   4771.50   6251.80   3474.37   1260.69
(shared)     rate_limit.acquire      1.20      1.30      1.40      1.32      1.49
```

(Units: microseconds. p95 means 95th percentile.)

## Interpretation

- For **trivial queries** (`SELECT 1`, `SELECT NOW()`), dbread overhead *can* exceed DB time. This is expected for any AST-based gateway and is the price of syntactic safety. If latency matters more than safety — don't use dbread.
- For **realistic queries**, overhead is a small fraction of typical DB time (which is milliseconds-to-seconds for anything touching data).
- For **complex queries**, the parser is O(tokens) × O(dialect-rules); the numbers scale linearly with statement size.
- `inject_limit` is ~2× `validate` because it re-parses + re-serialises. A future optimisation could reuse the AST from validate, but we deliberately keep them decoupled so `validate` never mutates AST.

## Disclaimer

Hardware, Python version, and workload shape all matter. Don't quote these numbers as authoritative for your environment — run the script on the target box.
