---
phase: 02
title: "Core Foundation — config + connections + audit"
status: pending
priority: P1
effort: 3h
created: 2026-04-22
---

# Phase 02 — Core Foundation

## Context Links

- Brainstorm §4.7 Config Sample, §4.8 Audit Record: [../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md](../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md)
- Previous: [phase-01-setup-scaffolding.md](phase-01-setup-scaffolding.md)

## Overview

- **Priority:** P1 (foundation cho SQL Guard, Rate Limiter, MCP Tools)
- **Status:** pending
- **Description:** Implement 3 module nền: `config.py` (typed settings từ YAML + env), `connections.py` (SQLAlchemy engine manager per-dialect với statement timeout), `audit.py` (JSONL append với rotation).

## Key Insights

- `url_env` pattern cho phép tách secret khỏi config file → safer commit
- `pool_pre_ping=True` tránh stale connection khi DB restart
- `statement_timeout` set qua `connect_args` khác nhau theo dialect (PG: `options='-c statement_timeout=30s'`, MySQL: `init_command='SET SESSION max_execution_time=30000'`, MSSQL: query hint `OPTION (QUERY_GOVERNOR_COST_LIMIT)`, Oracle: profile-level preferred)
- JSONL append-only → resilient crash, mỗi dòng 1 event → grep/jq friendly
- Rotation đơn giản: check size mỗi N write hoặc trước mỗi write, rename `audit.jsonl → audit.jsonl.1` khi > `rotate_mb`

## Requirements

**Functional:**
- Load `config.yaml` + env → typed `Settings` object
- Lazy-create SQLAlchemy engine per connection name
- `ConnectionManager.list_connections()` → list of (name, dialect)
- `AuditLogger.log(conn, sql, status, rows, ms, reason?)` append JSONL
- Rotation trigger khi file vượt `rotate_mb`

**Non-functional:**
- Config errors → clear message (e.g. "connection 'x' missing url/url_env")
- Audit write < 5ms p99 (local disk)
- Thread-safe audit write (MCP có thể concurrent)
- Each file < 200 LOC

## Architecture

### config.py

```
YAML file + os.environ
        ↓
pydantic-settings load
        ↓
Settings
├── connections: dict[str, ConnectionConfig]
│       ├── url: str | None
│       ├── url_env: str | None
│       ├── dialect: Literal['postgres','mysql','mssql','sqlite','oracle']
│       ├── rate_limit_per_min: int = 60
│       ├── statement_timeout_s: int = 30
│       └── max_rows: int = 1000
└── audit: AuditConfig
        ├── path: str = "./audit.jsonl"
        └── rotate_mb: int = 50
```

Validator: đúng 1 trong `url` hoặc `url_env` non-empty. Nếu `url_env` set → resolve từ `os.environ`, raise nếu missing.

### connections.py

```
ConnectionManager
├── __init__(settings)
├── _engines: dict[str, Engine]         # lazy init
├── get_engine(name) → Engine
│       ├── build connect_args by dialect
│       ├── create_engine(url, pool_pre_ping=True, connect_args=...)
│       └── cache in _engines
├── list_connections() → list[tuple[name, dialect]]
└── close_all()                          # on shutdown
```

### audit.py

```
AuditLogger
├── __init__(path, rotate_mb)
├── _lock: threading.Lock
├── log(conn, sql, status, rows, ms, reason=None)
│       ├── build dict (ts ISO8601 +07, conn, sql, rows, ms, status, reason)
│       ├── acquire lock
│       ├── check size → rotate if needed
│       ├── append json.dumps + '\n'
│       └── release
└── _rotate()                            # rename .jsonl → .jsonl.1 (overwrite old)
```

## Related Code Files

**Create:**
- `src/dbread/config.py` (~80 LOC)
- `src/dbread/connections.py` (~100 LOC)
- `src/dbread/audit.py` (~60 LOC)
- `tests/test_config.py`
- `tests/test_audit.py`
- `tests/test_connections.py`
- `tests/conftest.py` (shared fixtures: tmp_path config.yaml, env setup)

**Modify:** none
**Delete:** none

## Implementation Steps

1. **`config.py`** — pydantic models:
   ```python
   from pydantic import BaseModel, model_validator
   from pydantic_settings import BaseSettings
   import os, yaml
   from typing import Literal

   class ConnectionConfig(BaseModel):
       url: str | None = None
       url_env: str | None = None
       dialect: Literal['postgres','mysql','mssql','sqlite','oracle']
       rate_limit_per_min: int = 60
       statement_timeout_s: int = 30
       max_rows: int = 1000

       @model_validator(mode='after')
       def check_url(self):
           if bool(self.url) == bool(self.url_env):
               raise ValueError("exactly one of 'url' or 'url_env' required")
           return self

       def resolved_url(self) -> str:
           if self.url: return self.url
           v = os.environ.get(self.url_env)
           if not v: raise ValueError(f"env {self.url_env} not set")
           return v

   class AuditConfig(BaseModel):
       path: str = "./audit.jsonl"
       rotate_mb: int = 50

   class Settings(BaseModel):
       connections: dict[str, ConnectionConfig]
       audit: AuditConfig = AuditConfig()

       @classmethod
       def load(cls, path: str = "config.yaml") -> "Settings":
           with open(path) as f:
               raw = yaml.safe_load(f)
           return cls(**raw)
   ```

2. **`connections.py`** — engine manager với per-dialect connect_args:
   ```python
   from sqlalchemy import create_engine
   from sqlalchemy.engine import Engine
   from .config import ConnectionConfig, Settings

   DIALECT_TIMEOUT_ARGS = {
       'postgres': lambda s: {'options': f'-c statement_timeout={s*1000}'},
       'mysql': lambda s: {'init_command': f'SET SESSION max_execution_time={s*1000}'},
       'mssql': lambda s: {'timeout': s},  # via pyodbc connection timeout (query govern via hint later)
       'sqlite': lambda s: {},  # no server-side timeout
       'oracle': lambda s: {},  # handled via profile (docs)
   }

   class ConnectionManager:
       def __init__(self, settings: Settings):
           self.settings = settings
           self._engines: dict[str, Engine] = {}

       def get_engine(self, name: str) -> Engine:
           if name in self._engines:
               return self._engines[name]
           cfg = self.settings.connections.get(name)
           if not cfg:
               raise KeyError(f"unknown connection: {name}")
           connect_args = DIALECT_TIMEOUT_ARGS[cfg.dialect](cfg.statement_timeout_s)
           eng = create_engine(cfg.resolved_url(), pool_pre_ping=True, connect_args=connect_args)
           self._engines[name] = eng
           return eng

       def list_connections(self) -> list[tuple[str, str]]:
           return [(n, c.dialect) for n, c in self.settings.connections.items()]

       def close_all(self):
           for e in self._engines.values():
               e.dispose()
           self._engines.clear()
   ```

3. **`audit.py`** — JSONL with rotation:
   ```python
   import json, os, threading
   from datetime import datetime, timezone, timedelta

   TZ = timezone(timedelta(hours=7))

   class AuditLogger:
       def __init__(self, path: str, rotate_mb: int):
           self.path = path
           self.rotate_bytes = rotate_mb * 1024 * 1024
           self._lock = threading.Lock()

       def log(self, conn: str, sql: str, status: str, rows: int, ms: int, reason: str | None = None):
           rec = {
               "ts": datetime.now(TZ).isoformat(timespec='seconds'),
               "conn": conn, "sql": sql, "rows": rows, "ms": ms, "status": status,
           }
           if reason: rec["reason"] = reason
           line = json.dumps(rec, ensure_ascii=False) + "\n"
           with self._lock:
               self._maybe_rotate()
               with open(self.path, "a", encoding="utf-8") as f:
                   f.write(line)

       def _maybe_rotate(self):
           try:
               if os.path.getsize(self.path) >= self.rotate_bytes:
                   backup = self.path + ".1"
                   if os.path.exists(backup): os.remove(backup)
                   os.rename(self.path, backup)
           except FileNotFoundError:
               pass
   ```

4. **Tests** — `tests/test_config.py`:
   - Load valid YAML → Settings OK
   - Missing both `url` and `url_env` → ValidationError
   - Both set → ValidationError
   - `url_env` but env missing → `resolved_url()` raises
   - Defaults applied khi không set rate/timeout/max_rows

5. **Tests** — `tests/test_audit.py`:
   - Log 1 record → file có 1 line, parse JSON OK
   - Multi-thread log 100 records → file có 100 lines, no corruption
   - Size exceeds rotate_mb (small limit e.g. 1KB) → file rotated, `.1` exists

6. **Tests** — `tests/test_connections.py`:
   - Build SQLite engine (in-memory, dialect='sqlite') → `get_engine` works
   - `list_connections()` returns correct tuples
   - Unknown name → KeyError
   - PG connect_args test: kiểm `options` string có statement_timeout (mock create_engine)

7. **Run tests** — `uv run pytest tests/ -v` → all pass.

## Todo List

- [ ] Implement `src/dbread/config.py`
- [ ] Implement `src/dbread/connections.py`
- [ ] Implement `src/dbread/audit.py`
- [ ] Write `tests/conftest.py` (fixtures)
- [ ] Write `tests/test_config.py` (6+ cases)
- [ ] Write `tests/test_audit.py` (rotation + thread safety)
- [ ] Write `tests/test_connections.py`
- [ ] `uv run pytest` → all green
- [ ] `uv run ruff check src/` → no lint error
- [ ] Each file < 200 LOC

## Success Criteria

- `Settings.load("config.example.yaml")` (với env set) returns valid object
- `ConnectionManager` lazy-inits, cache hit verified
- `AuditLogger` survives 100 concurrent writes without corrupt
- Rotation kicks in at size threshold
- Test coverage > 80% cho 3 module

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| PG `statement_timeout` qua connect_args không work trên mọi driver version | Medium | Medium | Fallback set trong DB user profile (docs) — Layer 0 |
| MySQL `init_command` raise nếu user không có SET privilege | Low | Medium | Docs note + graceful degrade (warn log) |
| Audit rotation race giữa threads | Low | Low | `threading.Lock` cover cả rotate + append |
| Config YAML invalid → crash on startup | High | Low | Pydantic raise clear ValidationError |
| File handle leak | Low | Low | Use `with open(...)` cho mỗi write |

## Security Considerations

- **Secrets in memory:** `resolved_url()` returns string chứa password — không log/print
- **Audit path permission:** user chịu trách nhiệm, docs note `chmod 600 audit.jsonl`
- **SQL in audit:** log raw SQL có thể chứa PII → docs warn cân nhắc trước prod
- **Engine URL không log:** `SQLAlchemy` default không log password, nhưng set `echo=False`

## Next Steps

- **Blocks:** Phase 03, 04, 05 (tất cả cần config + connection + audit)
- **Dependencies:** Phase 01 (pyproject.toml có deps)
- **Follow-up:** Phase 03 (SQL Guard) bắt đầu
