---
phase: 07
title: "Integration Tests — E2E with real DBs"
status: pending
priority: P2
effort: 3h
created: 2026-04-22
---

# Phase 07 — Integration Tests

## Context Links

- Brainstorm §7 Success Criteria: [../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md](../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md)
- Previous: [phase-06-documentation.md](phase-06-documentation.md)

## Overview

- **Priority:** P2 (validation, not blocker — unit tests cover core logic)
- **Status:** pending
- **Description:** End-to-end tests against real Postgres + MySQL (Docker) + SQLite (file). Verify full MCP pipeline: guard → rate limit → execute → audit. Plus manual smoke test with Claude Code.

## Key Insights

- Docker Compose spins PG + MySQL quickly; SQLite in-memory for speed
- Seed minimal schema `users(id, name)`, `orders(id, user_id, total)` với ~3-5 rows
- Test via direct `ToolHandlers` instance (simpler than MCP stdio round-trip); optionally one `mcp.Client` based test to verify transport
- Rate limit real timing test: set `rate_limit_per_min=60` → call 61 times tight loop → 61st reject; no sleep hack needed
- Audit verification: parse `audit.jsonl` line-by-line, assert expected records
- Manual Claude Code test = human-in-loop, document steps

## Requirements

**Functional:**
- Docker compose up → fresh PG + MySQL ready
- Seed scripts create RO user + schema + rows
- E2E happy path (query returns rows) verified on PG + MySQL + SQLite
- E2E reject paths verified (DML, CTE-DML, multi-statement)
- Rate limit actually triggers at configured threshold
- Audit file has expected lines
- Manual Claude Code smoke doc

**Non-functional:**
- Test suite skippable if Docker unavailable (`pytest.mark.docker`)
- Test isolated (each test = fresh container or fresh SQLite file)

## Architecture

```
tests/integration/
├── docker-compose.yml           # PG + MySQL services
├── fixtures/
│   ├── pg-init.sql              # CREATE USER ai_readonly, schema, seed
│   ├── mysql-init.sql           # same
│   └── sqlite-seed.py           # build tmp SQLite with rows
├── conftest.py                  # pytest fixtures: start containers, seed
├── test_e2e_pg.py
├── test_e2e_mysql.py
├── test_e2e_sqlite.py
└── test_e2e_mcp_transport.py    # optional: stdio round-trip
```

## Related Code Files

**Create:**
- `tests/integration/docker-compose.yml`
- `tests/integration/fixtures/pg-init.sql`
- `tests/integration/fixtures/mysql-init.sql`
- `tests/integration/fixtures/sqlite-seed.py`
- `tests/integration/conftest.py`
- `tests/integration/test_e2e_pg.py`
- `tests/integration/test_e2e_mysql.py`
- `tests/integration/test_e2e_sqlite.py`
- `tests/integration/test_e2e_mcp_transport.py` (optional)
- `docs/manual-smoke-test.md`

**Modify:**
- `pyproject.toml` — add `pytest-docker` or `testcontainers` to dev extras

**Delete:** none

## Implementation Steps

### Step 1 — `docker-compose.yml`

```yaml
version: "3.8"
services:
  pg:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: admin
      POSTGRES_PASSWORD: adminpw
      POSTGRES_DB: testdb
    ports: ["54329:5432"]    # avoid clash with local PG
    volumes:
      - ./fixtures/pg-init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    healthcheck:
      test: ["CMD", "pg_isready", "-U", "admin"]
      interval: 2s
      timeout: 2s
      retries: 20

  mysql:
    image: mysql:8.4
    environment:
      MYSQL_ROOT_PASSWORD: adminpw
      MYSQL_DATABASE: testdb
    ports: ["33069:3306"]
    volumes:
      - ./fixtures/mysql-init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-padminpw"]
      interval: 2s
      timeout: 2s
      retries: 20
```

### Step 2 — `fixtures/pg-init.sql`

```sql
-- Runs as superuser in postgres entrypoint
CREATE USER ai_readonly WITH PASSWORD 'ropw';
GRANT CONNECT ON DATABASE testdb TO ai_readonly;
\c testdb
GRANT USAGE ON SCHEMA public TO ai_readonly;

CREATE TABLE users (id SERIAL PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE orders (id SERIAL PRIMARY KEY, user_id INT REFERENCES users(id), total NUMERIC);
INSERT INTO users(name) VALUES ('alice'),('bob'),('carol');
INSERT INTO orders(user_id, total) VALUES (1, 100),(1, 200),(2, 50);

GRANT SELECT ON ALL TABLES IN SCHEMA public TO ai_readonly;
ALTER USER ai_readonly SET default_transaction_read_only = on;
ALTER USER ai_readonly SET statement_timeout = '5s';
```

### Step 3 — `fixtures/mysql-init.sql`

```sql
CREATE USER 'ai_readonly'@'%' IDENTIFIED BY 'ropw';
GRANT SELECT, SHOW VIEW ON testdb.* TO 'ai_readonly'@'%';

USE testdb;
CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(64) NOT NULL);
CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, user_id INT, total DECIMAL(10,2), FOREIGN KEY (user_id) REFERENCES users(id));
INSERT INTO users(name) VALUES ('alice'),('bob'),('carol');
INSERT INTO orders(user_id, total) VALUES (1, 100),(1, 200),(2, 50);
```

### Step 4 — `conftest.py`

```python
import pytest, time, subprocess, os, pathlib
from sqlalchemy import create_engine, text

HERE = pathlib.Path(__file__).parent
COMPOSE = HERE / "docker-compose.yml"

def _docker(*args):
    return subprocess.run(["docker", "compose", "-f", str(COMPOSE), *args], check=True)

@pytest.fixture(scope="session")
def pg_url():
    if os.environ.get("SKIP_DOCKER"): pytest.skip("SKIP_DOCKER")
    _docker("up", "-d", "pg")
    url = "postgresql+psycopg2://ai_readonly:ropw@localhost:54329/testdb"
    _wait_ready(url)
    yield url
    _docker("down", "-v")

@pytest.fixture(scope="session")
def mysql_url():
    if os.environ.get("SKIP_DOCKER"): pytest.skip("SKIP_DOCKER")
    _docker("up", "-d", "mysql")
    url = "mysql+pymysql://ai_readonly:ropw@localhost:33069/testdb"
    _wait_ready(url)
    yield url

def _wait_ready(url, timeout=60):
    eng = create_engine(url)
    for _ in range(timeout):
        try:
            with eng.connect() as c: c.execute(text("SELECT 1"))
            return
        except Exception:
            time.sleep(1)
    raise TimeoutError(url)

@pytest.fixture
def handlers_for(pg_url, tmp_path):
    # helper to build ToolHandlers for a given DB URL
    from dbread.config import Settings, ConnectionConfig, AuditConfig
    from dbread.connections import ConnectionManager
    from dbread.sql_guard import SqlGuard
    from dbread.rate_limiter import RateLimiter
    from dbread.audit import AuditLogger
    from dbread.tools import ToolHandlers

    def _make(url, dialect, rate=60, max_rows=100):
        s = Settings(
            connections={"t": ConnectionConfig(url=url, dialect=dialect, rate_limit_per_min=rate, max_rows=max_rows)},
            audit=AuditConfig(path=str(tmp_path/"audit.jsonl"), rotate_mb=1),
        )
        cm = ConnectionManager(s)
        return ToolHandlers(s, cm, SqlGuard(), RateLimiter(s), AuditLogger(s.audit.path, 1)), tmp_path/"audit.jsonl"
    return _make
```

### Step 5 — `test_e2e_pg.py`

```python
import json, pytest
from dbread.tools import ToolError

pytestmark = pytest.mark.docker

def test_pg_query_happy(handlers_for, pg_url):
    h, audit = handlers_for(pg_url, "postgres")
    r = h.query("t", "SELECT * FROM users")
    assert r["row_count"] == 3
    assert "alice" in [row[1] for row in r["rows"]]

def test_pg_rejects_update(handlers_for, pg_url):
    h, audit = handlers_for(pg_url, "postgres")
    with pytest.raises(ToolError, match="sql_guard"):
        h.query("t", "UPDATE users SET name='x'")
    # audit contains rejection
    lines = audit.read_text().strip().split("\n")
    rec = json.loads(lines[-1])
    assert rec["status"] == "rejected" and "Update" in rec["reason"]

def test_pg_rejects_cte_dml(handlers_for, pg_url):
    h, _ = handlers_for(pg_url, "postgres")
    with pytest.raises(ToolError):
        h.query("t", "WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d")

def test_pg_even_if_parser_bypassed_db_blocks(handlers_for, pg_url):
    # Layer 0 verification: even hypothetical bypass, DB RO user blocks
    # (We can't easily bypass our own guard, so simulate by calling engine directly)
    h, _ = handlers_for(pg_url, "postgres")
    eng = h.cm.get_engine("t")
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy import text
    with pytest.raises(DBAPIError):
        with eng.connect() as c:
            c.execute(text("UPDATE users SET name='x'"))

def test_pg_rate_limit(handlers_for, pg_url):
    h, _ = handlers_for(pg_url, "postgres", rate=2, max_rows=10)  # burst=2
    h.query("t", "SELECT 1")
    h.query("t", "SELECT 1")
    with pytest.raises(ToolError, match="rate_limit"):
        h.query("t", "SELECT 1")

def test_pg_statement_timeout(handlers_for, pg_url):
    h, _ = handlers_for(pg_url, "postgres")
    with pytest.raises(ToolError, match="db_error"):
        h.query("t", "SELECT pg_sleep(10)")  # pg-init set statement_timeout=5s

def test_pg_limit_inject(handlers_for, pg_url):
    h, audit = handlers_for(pg_url, "postgres", max_rows=2)
    r = h.query("t", "SELECT * FROM users")
    assert r["row_count"] == 2
    assert r["truncated"] is True

def test_pg_list_tables(handlers_for, pg_url):
    h, _ = handlers_for(pg_url, "postgres")
    t = h.list_tables("t")
    assert "users" in t and "orders" in t

def test_pg_describe_table(handlers_for, pg_url):
    h, _ = handlers_for(pg_url, "postgres")
    d = h.describe_table("t", "users")
    names = [c["name"] for c in d["columns"]]
    assert "id" in names and "name" in names
    assert any(c["pk"] for c in d["columns"] if c["name"] == "id")

def test_pg_explain(handlers_for, pg_url):
    h, _ = handlers_for(pg_url, "postgres")
    r = h.explain("t", "SELECT * FROM users WHERE id = 1")
    assert len(r["plan"]) > 0
```

### Step 6 — `test_e2e_mysql.py`

Mirror PG tests adjusted for MySQL dialect:
- `UPDATE` rejected by guard
- `SHOW TABLES` works via `list_tables`
- `SELECT SLEEP(10)` timeout (set via `init_command`)

### Step 7 — `test_e2e_sqlite.py`

- No Docker needed, use tmp file
- Seed users/orders, run same happy path
- Test `mode=ro` URL rejects even direct write

### Step 8 — `test_e2e_mcp_transport.py` (optional)

Use MCP SDK's test client to spawn subprocess `dbread` + send initialize + call_tool:
```python
import asyncio
from mcp.client.stdio import stdio_client, StdioServerParameters

async def test_mcp_roundtrip():
    params = StdioServerParameters(command="uv", args=["run", "dbread"], env={"DBREAD_CONFIG": "tests/integration/fixtures/test-config.yaml"})
    async with stdio_client(params) as (r, w):
        # send initialize, list_tools, call_tool query, assert response
        ...
```

Skip if complex; unit tests + direct handler tests cover most.

### Step 9 — `docs/manual-smoke-test.md`

```markdown
# Manual Smoke Test (Claude Code)

## Pre-req
- dbread installed, config.yaml points at test DB (PG or SQLite)
- Claude Code MCP config registers dbread

## Steps
1. Start Claude Code session
2. Ask: "List my configured DB connections"
   - Expected: Claude calls `list_connections`, returns names
3. Ask: "Show tables in <connection>"
   - Expected: `list_tables` result
4. Ask: "Show first 5 rows of users"
   - Expected: `query` with LIMIT injected, rows returned
5. Ask: "Delete user with id 1"
   - Expected: Claude tries `query` with DELETE, gets `sql_guard: node_rejected: Delete`; Claude reports limitation gracefully
6. Check `audit.jsonl`: grep for `"status":"rejected"` → 1 entry for DELETE attempt

## Pass Criteria
- All 6 steps behave as expected
- No password/DSN in audit log
- MCP server stderr log clean (no crashes)
```

### Step 10 — Run + verify

```bash
cd tests/integration
docker compose up -d
cd ../..
uv run pytest tests/integration/ -v -m docker
docker compose -f tests/integration/docker-compose.yml down -v
```

## Todo List

- [ ] Create `docker-compose.yml` with PG + MySQL services
- [ ] Create `fixtures/pg-init.sql` — RO user + schema + seed
- [ ] Create `fixtures/mysql-init.sql` — same
- [ ] Create `tests/integration/conftest.py` — fixtures + compose lifecycle
- [ ] Write `test_e2e_pg.py` — 9+ cases
- [ ] Write `test_e2e_mysql.py` — mirror
- [ ] Write `test_e2e_sqlite.py` — tmp file
- [ ] (optional) `test_e2e_mcp_transport.py` stdio round-trip
- [ ] Write `docs/manual-smoke-test.md`
- [ ] Run `uv run pytest tests/integration/ -m docker` — all pass
- [ ] Manual smoke test with real Claude Code → document result
- [ ] Update README "Development" section with integration test command

## Success Criteria

- Docker compose up/down clean
- PG happy path: query returns rows, rate limit triggers, timeout triggers
- MySQL happy path: same
- SQLite happy path: works without Docker
- Every reject case (UPDATE, CTE-DML, multi-statement, blacklist fn) verified on at least PG
- Audit file has exactly the expected rejection + ok lines
- Manual smoke with Claude Code passes all 6 steps

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Docker unavailable on CI/dev machine | Medium | Low | `pytest.mark.docker` + `SKIP_DOCKER` env; SQLite tests always run |
| Port clash (5432, 3306) | Medium | Low | Use non-default ports (54329, 33069) |
| PG/MySQL image updates break fixtures | Low | Low | Pin major version `16-alpine`, `8.4` |
| statement_timeout test flaky on slow CI | Low | Low | Use generous SLEEP(10) vs 5s threshold; retry |
| MCP stdio test complexity | Medium | Medium | Mark optional; direct handler tests cover semantics |

## Security Considerations

- Integration test credentials `ropw` — test-only, never outside test env
- Docker ports localhost-bind (default), not exposed externally
- `docker compose down -v` tears down volumes → no residual data

## Next Steps

- **Blocks:** Release readiness
- **Dependencies:** Phase 05 (tools), Phase 06 (docs for manual smoke)
- **Follow-up:**
  - GitHub Actions workflow running integration tests
  - MSSQL + Oracle integration tests (Docker images exist, skipped for initial scope)
  - Publish to PyPI / uv index after smoke test passes
