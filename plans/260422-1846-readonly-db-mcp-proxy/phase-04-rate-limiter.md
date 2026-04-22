---
phase: 04
title: "Rate Limiter — token bucket per connection"
status: pending
priority: P1
effort: 2h
created: 2026-04-22
---

# Phase 04 — Rate Limiter

## Context Links

- Brainstorm §4.1 Defense in Depth Layer 2, §6 Risks: [../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md](../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md)
- Previous: [phase-03-sql-guard.md](phase-03-sql-guard.md)

## Overview

- **Priority:** P1 (prevent runaway loop)
- **Status:** pending
- **Description:** Implement `TokenBucket` per-connection in-memory rate limiter. Configurable `rate_per_min` + `burst`. Thread-safe non-blocking `acquire()` for MCP multi-request scenarios.

## Key Insights

- **Token bucket** (not leaky bucket) cho phép burst nhỏ nhưng cap total rate → phù hợp AI exploratory queries (spike đầu, calm sau)
- **Per connection name** (not global) — `analytics_prod` limit 60/min không ảnh hưởng `local_mysql` 120/min
- **In-memory** — MCP server là single-process, không cần Redis. Restart = reset bucket (acceptable for local scope)
- **Non-blocking `acquire()`** — return False thay vì sleep; MCP tool response nên nhanh, caller (AI) nhận error và retry sau
- **Thread-safe** — `threading.Lock` bao quanh token calc; MCP SDK có thể concurrent tool calls
- **Refill lazy** — compute tokens chỉ khi acquire gọi: `tokens += elapsed * (rate/60); tokens = min(tokens, burst)` (deprecated? use capacity)

## Requirements

**Functional:**
- `TokenBucket(rate_per_min, burst)` — init
- `acquire() -> bool` — consume 1 token, return True if OK else False
- `RateLimiter` manager: dict of buckets keyed by connection name, lazy create
- `RateLimiter.acquire(connection_name) -> bool`

**Non-functional:**
- Thread-safe
- O(1) acquire
- File < 100 LOC

## Architecture

```
TokenBucket
├── rate_per_sec: float         # rate_per_min / 60
├── capacity: float             # burst (default = rate_per_min)
├── tokens: float               # current
├── last_refill: float          # monotonic time
├── lock: threading.Lock
└── acquire() → bool
      with lock:
          now = time.monotonic()
          elapsed = now - last_refill
          tokens = min(capacity, tokens + elapsed * rate_per_sec)
          last_refill = now
          if tokens >= 1:
              tokens -= 1; return True
          return False

RateLimiter
├── _buckets: dict[str, TokenBucket]
├── _lock: threading.Lock              # for bucket creation
├── settings: Settings
└── acquire(name) → bool
      bucket = _get_or_create(name)
      return bucket.acquire()
```

## Related Code Files

**Create:**
- `src/dbread/rate_limiter.py` (~80 LOC)
- `tests/test_rate_limiter.py`

**Modify:** none

## Implementation Steps

1. **`rate_limiter.py`** — `TokenBucket` class:
   ```python
   import threading
   import time
   from .config import Settings

   class TokenBucket:
       def __init__(self, rate_per_min: int, burst: int | None = None):
           self.rate_per_sec = rate_per_min / 60.0
           self.capacity = float(burst if burst is not None else rate_per_min)
           self.tokens = self.capacity
           self.last_refill = time.monotonic()
           self.lock = threading.Lock()

       def acquire(self) -> bool:
           with self.lock:
               now = time.monotonic()
               elapsed = now - self.last_refill
               self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
               self.last_refill = now
               if self.tokens >= 1.0:
                   self.tokens -= 1.0
                   return True
               return False
   ```

2. **`RateLimiter` manager**:
   ```python
   class RateLimiter:
       def __init__(self, settings: Settings):
           self.settings = settings
           self._buckets: dict[str, TokenBucket] = {}
           self._lock = threading.Lock()

       def acquire(self, connection: str) -> bool:
           bucket = self._buckets.get(connection)
           if bucket is None:
               with self._lock:
                   bucket = self._buckets.get(connection)
                   if bucket is None:
                       cfg = self.settings.connections[connection]
                       bucket = TokenBucket(cfg.rate_limit_per_min)
                       self._buckets[connection] = bucket
           return bucket.acquire()
   ```

3. **Tests — `tests/test_rate_limiter.py`**:

### Test Matrix

| # | Scenario | Assertion |
|---|----------|-----------|
| R1 | Fresh bucket rate=60, burst=60 → acquire 60 times | all True |
| R2 | After burst exhausted → 61st acquire | False |
| R3 | After 1 second wait (rate=60 → +1 token/sec) → acquire | True |
| R4 | Multiple connections: `a` rate=60, `b` rate=120 independent | exhaust `a` doesn't affect `b` |
| R5 | Threaded 100 acquires, bucket capacity 60 → exactly 60 True | (atomic, no double-spend) |
| R6 | Low rate=6/min (0.1/s), immediate 7 acquires | 6 True + 1 False |
| R7 | After exhaust, wait 10s (rate=60 → +10 tokens cap at 60) | next 10 acquire True |
| R8 | Time freeze (mock monotonic): refill stopped | behaves correctly with controlled time |

4. **Test implementation sample**:
   ```python
   import threading
   from dbread.rate_limiter import TokenBucket, RateLimiter

   def test_burst_then_deny():
       b = TokenBucket(rate_per_min=60, burst=60)
       assert all(b.acquire() for _ in range(60))
       assert b.acquire() is False

   def test_refill_after_wait(monkeypatch):
       t = [0.0]
       monkeypatch.setattr('time.monotonic', lambda: t[0])
       b = TokenBucket(rate_per_min=60)
       assert all(b.acquire() for _ in range(60))
       assert not b.acquire()
       t[0] = 2.0  # +2s = +2 tokens
       assert b.acquire()
       assert b.acquire()
       assert not b.acquire()

   def test_thread_safety():
       b = TokenBucket(rate_per_min=60, burst=60)
       results = []
       def worker():
           results.append(b.acquire())
       threads = [threading.Thread(target=worker) for _ in range(100)]
       for t in threads: t.start()
       for t in threads: t.join()
       assert sum(results) == 60  # exactly 60 True

   def test_independent_buckets(tmp_path):
       # use fake Settings with 2 connections
       ...
       rl = RateLimiter(settings)
       # exhaust 'a'
       for _ in range(60): rl.acquire('a')
       assert not rl.acquire('a')
       # 'b' still fresh
       assert rl.acquire('b')
   ```

5. **Run tests** — `uv run pytest tests/test_rate_limiter.py -v` → all pass.

## Todo List

- [ ] Implement `src/dbread/rate_limiter.py` — `TokenBucket` + `RateLimiter`
- [ ] Write `tests/test_rate_limiter.py` cover 8+ scenarios
- [ ] Burst + deny test (R1, R2)
- [ ] Refill timing test (R3, R7) — use monkeypatch `time.monotonic`
- [ ] Multi-connection independence (R4)
- [ ] Thread safety test (R5) — 100 threads
- [ ] Low rate precision (R6)
- [ ] `uv run pytest tests/test_rate_limiter.py` all pass
- [ ] File < 100 LOC

## Success Criteria

- Bucket behavior toán đúng token count qua mọi scenario
- Zero double-spend với 100 concurrent threads
- Per-connection independent verified
- Coverage ≥ 85%

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Race condition under extreme load | Low | Medium | `threading.Lock` bao toàn bộ token calc |
| User bypass bằng spawn nhiều MCP session | Medium | Low | Limit per process, docs note "rate limit is per-MCP-process" |
| Monotonic clock jump on laptop sleep | Low | Low | `time.monotonic()` không bị clock adjust, nhưng laptop resume có thể có skip — acceptable (phải extra tokens, không giảm) |
| Memory grow với nhiều connection names | Low | Low | Config giới hạn connection count; docs khuyến cáo < 20 |

## Security Considerations

- **Bypass via multiple clients:** nếu user chạy 2 Claude Code sessions → mỗi session có MCP server riêng → rate limit tách. Docs note scope.
- **DoS surface:** rate limit bảo vệ DB, không bảo vệ MCP server khỏi spam. Nếu AI spam, MCP chỉ nhanh chóng reject — CPU impact low.
- **No persistence:** restart MCP = reset bucket → user có thể "reset" bằng restart. Acceptable for local scope (brainstorm §2).

## Next Steps

- **Blocks:** Phase 05 (MCP Tools wire rate_limiter.acquire() trước execute)
- **Dependencies:** Phase 02 (uses `Settings`)
- **Follow-up:** Phase 05 integration
