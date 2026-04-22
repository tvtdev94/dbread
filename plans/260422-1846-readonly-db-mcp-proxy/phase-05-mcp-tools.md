---
phase: 05
title: "MCP Tools Wiring — 5 tools + server entry"
status: pending
priority: P1
effort: 3h
created: 2026-04-22
---

# Phase 05 — MCP Tools Wiring

## Context Links

- Brainstorm §4.5 MCP Tools, §4.2 Architecture: [../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md](../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md)
- Previous: [phase-04-rate-limiter.md](phase-04-rate-limiter.md)
- MCP SDK docs: https://modelcontextprotocol.io/docs/concepts/tools

## Overview

- **Priority:** P1 (end-user entry point)
- **Status:** pending
- **Description:** Wire 5 MCP tools (`list_connections`, `list_tables`, `describe_table`, `query`, `explain`) chains guard + rate limiter + engine execute + audit. Build `server.py` MCP stdio server entry, register tools, lifecycle hooks.

## Key Insights

- MCP SDK Python dùng `@server.list_tools()`, `@server.call_tool()` decorator pattern
- Tool schema JSON Schema 7 spec — describe input/output for AI to invoke correctly
- stdio transport: MCP server đọc stdin/ghi stdout; log đi stderr (không được ghi stdout — sẽ break protocol)
- Tool response: `list[TextContent | ImageContent]` — use `TextContent(type='text', text=json.dumps(result))` cho rows
- Execute timeout: SQLAlchemy `connection.execution_options(timeout=...)` không universal — phần lớn handled bởi DB-side `statement_timeout` (Phase 02 setup)
- **Flow mỗi tool execute:**
  1. Resolve connection → engine
  2. (query/explain only) `guard.validate()` → reject → audit log + return error
  3. (query only) `guard.inject_limit()`
  4. `rate_limiter.acquire()` → fail → return rate_limit error
  5. Execute query qua engine, measure ms
  6. `audit.log()` status=ok/failed
  7. Return rows/schema JSON (rows limit `max_rows`)

## Requirements

**Functional:**
- 5 MCP tools exposed với JSON schema mô tả
- Tool chain: guard → limit → rate → execute → audit
- `describe_table` / `list_tables` dùng SQLAlchemy `inspect()` — không raw SQL → không cần guard (inspect chỉ SELECT metadata)
- `explain` qua guard (EXPLAIN token được ALLOW)
- Error responses user-friendly

**Non-functional:**
- Tool response < 500ms (ex DB latency) — parser + audit overhead nhỏ
- Graceful error: DB down, invalid SQL, rate limited → structured error JSON
- Server file < 120 LOC, tools file < 200 LOC

## Architecture

### tools.py

```
ToolHandlers
├── __init__(conn_mgr, guard, rate_limiter, audit)
├── list_connections() → [{name, dialect}, ...]
├── list_tables(connection, schema=None) → [table_name, ...]
├── describe_table(connection, table, schema=None) → {columns:[{name,type,nullable,pk}], indexes:[...]}
├── query(connection, sql, max_rows=None) → {columns: [...], rows: [[...]], row_count: N, truncated: bool}
│       1. cfg = settings.connections[connection]
│       2. result = guard.validate(sql, cfg.dialect)
│          if not allowed: audit.log(conn, sql, 'rejected', 0, 0, result.reason); raise ToolError
│       3. effective_max = max_rows or cfg.max_rows
│       4. sql2 = guard.inject_limit(sql, cfg.dialect, effective_max)
│       5. if not rate_limiter.acquire(connection): audit.log(...rejected 'rate_limit'); raise
│       6. engine = conn_mgr.get_engine(connection); t0 = time.perf_counter()
│       7. try: rows = engine.execute(sql2).fetchmany(effective_max)
│          except: audit.log(...'failed', err); raise
│       8. ms = int((time.perf_counter()-t0)*1000)
│       9. audit.log(conn, sql2, 'ok', len(rows), ms)
│       10. return {columns, rows, row_count, truncated: len(rows)==effective_max}
└── explain(connection, sql) → {plan: [...]}
        similar to query but prefix 'EXPLAIN' (dialect-aware: PG='EXPLAIN ', MSSQL='SET SHOWPLAN_ALL ON', ...)
```

### server.py

```
main():
    settings = Settings.load(os.environ.get('DBREAD_CONFIG', 'config.yaml'))
    conn_mgr = ConnectionManager(settings)
    guard = SqlGuard()
    rl = RateLimiter(settings)
    audit = AuditLogger(settings.audit.path, settings.audit.rotate_mb)
    tools = ToolHandlers(conn_mgr, guard, rl, audit)
    server = Server("dbread")

    @server.list_tools()
    async def handle_list_tools(): return [Tool(name=..., description=..., inputSchema=...)]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict):
        handler = getattr(tools, name)
        result = handler(**arguments)
        return [TextContent(type='text', text=json.dumps(result, default=str))]

    async with stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(server_name='dbread', server_version='0.1.0'))
```

## Related Code Files

**Create:**
- `src/dbread/tools.py` (~180 LOC)
- `src/dbread/server.py` (~100 LOC)
- `tests/test_tools.py`

**Modify:**
- `pyproject.toml` entry point đã set Phase 01 (`dbread = "dbread.server:main"`)

**Delete:** none

## Implementation Steps

1. **`tools.py`** — `ToolHandlers` class:
   ```python
   import json, time
   from sqlalchemy import text, inspect
   from .config import Settings
   from .connections import ConnectionManager
   from .sql_guard import SqlGuard
   from .rate_limiter import RateLimiter
   from .audit import AuditLogger

   class ToolError(Exception): pass

   class ToolHandlers:
       def __init__(self, settings, conn_mgr, guard, rate_limiter, audit):
           self.settings = settings
           self.cm = conn_mgr
           self.guard = guard
           self.rl = rate_limiter
           self.audit = audit

       def list_connections(self) -> list[dict]:
           return [{"name": n, "dialect": d} for n, d in self.cm.list_connections()]

       def list_tables(self, connection: str, schema: str | None = None) -> list[str]:
           eng = self.cm.get_engine(connection)
           insp = inspect(eng)
           return insp.get_table_names(schema=schema)

       def describe_table(self, connection: str, table: str, schema: str | None = None) -> dict:
           eng = self.cm.get_engine(connection)
           insp = inspect(eng)
           cols = insp.get_columns(table, schema=schema)
           idxs = insp.get_indexes(table, schema=schema)
           pks = insp.get_pk_constraint(table, schema=schema).get('constrained_columns', [])
           return {
               "columns": [{"name": c["name"], "type": str(c["type"]), "nullable": c.get("nullable", True), "pk": c["name"] in pks} for c in cols],
               "indexes": [{"name": i["name"], "columns": i["column_names"], "unique": i.get("unique", False)} for i in idxs],
           }

       def query(self, connection: str, sql: str, max_rows: int | None = None) -> dict:
           cfg = self.settings.connections[connection]
           res = self.guard.validate(sql, cfg.dialect)
           if not res.allowed:
               self.audit.log(connection, sql, "rejected", 0, 0, res.reason)
               raise ToolError(f"sql_guard: {res.reason}")
           effective = max_rows if (max_rows and max_rows <= cfg.max_rows) else cfg.max_rows
           sql2 = self.guard.inject_limit(sql, cfg.dialect, effective)
           if not self.rl.acquire(connection):
               self.audit.log(connection, sql, "rejected", 0, 0, "rate_limit")
               raise ToolError("rate_limit_exceeded")
           eng = self.cm.get_engine(connection)
           t0 = time.perf_counter()
           try:
               with eng.connect() as c:
                   result = c.execute(text(sql2))
                   columns = list(result.keys())
                   rows = [list(r) for r in result.fetchmany(effective)]
           except Exception as e:
               ms = int((time.perf_counter() - t0) * 1000)
               self.audit.log(connection, sql2, "failed", 0, ms, str(e)[:200])
               raise ToolError(f"db_error: {e}")
           ms = int((time.perf_counter() - t0) * 1000)
           self.audit.log(connection, sql2, "ok", len(rows), ms)
           return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": len(rows) == effective}

       def explain(self, connection: str, sql: str) -> dict:
           cfg = self.settings.connections[connection]
           res = self.guard.validate(sql, cfg.dialect)
           if not res.allowed:
               self.audit.log(connection, sql, "rejected", 0, 0, res.reason)
               raise ToolError(f"sql_guard: {res.reason}")
           explain_sql = self._build_explain(sql, cfg.dialect)
           if not self.rl.acquire(connection):
               self.audit.log(connection, sql, "rejected", 0, 0, "rate_limit")
               raise ToolError("rate_limit_exceeded")
           eng = self.cm.get_engine(connection)
           t0 = time.perf_counter()
           with eng.connect() as c:
               plan = [list(r) for r in c.execute(text(explain_sql))]
           ms = int((time.perf_counter() - t0) * 1000)
           self.audit.log(connection, explain_sql, "ok", len(plan), ms)
           return {"plan": plan}

       @staticmethod
       def _build_explain(sql: str, dialect: str) -> str:
           if dialect == 'postgres': return f"EXPLAIN {sql}"
           if dialect == 'mysql': return f"EXPLAIN {sql}"
           if dialect == 'sqlite': return f"EXPLAIN QUERY PLAN {sql}"
           if dialect == 'mssql': return f"SET SHOWPLAN_TEXT ON; {sql}"  # best-effort
           if dialect == 'oracle': return f"EXPLAIN PLAN FOR {sql}"
           return f"EXPLAIN {sql}"
   ```

2. **`server.py`** — MCP entry:
   ```python
   import asyncio, json, os, sys, logging
   from mcp.server import Server, NotificationOptions
   from mcp.server.models import InitializationOptions
   from mcp.server.stdio import stdio_server
   from mcp.types import Tool, TextContent
   from .config import Settings
   from .connections import ConnectionManager
   from .sql_guard import SqlGuard
   from .rate_limiter import RateLimiter
   from .audit import AuditLogger
   from .tools import ToolHandlers, ToolError

   logging.basicConfig(level=logging.INFO, stream=sys.stderr)  # stderr only — stdout is MCP protocol
   log = logging.getLogger("dbread")

   TOOL_SCHEMAS = [
       Tool(name="list_connections", description="List configured DB connections", inputSchema={"type": "object", "properties": {}}),
       Tool(name="list_tables", description="List tables in a connection", inputSchema={
           "type": "object",
           "properties": {"connection": {"type": "string"}, "schema": {"type": "string"}},
           "required": ["connection"],
       }),
       Tool(name="describe_table", description="Describe columns and indexes of a table", inputSchema={
           "type": "object",
           "properties": {"connection": {"type": "string"}, "table": {"type": "string"}, "schema": {"type": "string"}},
           "required": ["connection", "table"],
       }),
       Tool(name="query", description="Run a read-only SQL query (SELECT/WITH). Auto-limited. Rate-limited. Audited.", inputSchema={
           "type": "object",
           "properties": {"connection": {"type": "string"}, "sql": {"type": "string"}, "max_rows": {"type": "integer"}},
           "required": ["connection", "sql"],
       }),
       Tool(name="explain", description="Return query plan (EXPLAIN) for a SELECT", inputSchema={
           "type": "object",
           "properties": {"connection": {"type": "string"}, "sql": {"type": "string"}},
           "required": ["connection", "sql"],
       }),
   ]

   async def run():
       settings = Settings.load(os.environ.get("DBREAD_CONFIG", "config.yaml"))
       cm = ConnectionManager(settings)
       guard = SqlGuard()
       rl = RateLimiter(settings)
       audit = AuditLogger(settings.audit.path, settings.audit.rotate_mb)
       handlers = ToolHandlers(settings, cm, guard, rl, audit)

       server = Server("dbread")

       @server.list_tools()
       async def _list(): return TOOL_SCHEMAS

       @server.call_tool()
       async def _call(name: str, arguments: dict):
           try:
               fn = getattr(handlers, name)
               result = fn(**(arguments or {}))
               return [TextContent(type="text", text=json.dumps(result, default=str, ensure_ascii=False))]
           except ToolError as e:
               return [TextContent(type="text", text=json.dumps({"error": str(e)}))]
           except Exception as e:
               log.exception("tool error")
               return [TextContent(type="text", text=json.dumps({"error": f"internal: {e}"}))]

       async with stdio_server() as (r, w):
           await server.run(r, w, InitializationOptions(
               server_name="dbread", server_version="0.1.0",
               capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={}),
           ))
       cm.close_all()

   def main():
       asyncio.run(run())

   if __name__ == "__main__":
       main()
   ```

3. **Tests — `tests/test_tools.py`**:

### Test Matrix

| # | Scenario | Setup | Assertion |
|---|----------|-------|-----------|
| T1 | `list_connections` with 2 configured | in-memory SQLite | returns 2 items |
| T2 | `list_tables` on seeded SQLite | table `users` | contains 'users' |
| T3 | `describe_table users` | cols id,name | columns match, pk detected |
| T4 | `query SELECT * FROM users` | 3 rows seeded | returns 3 rows, row_count=3 |
| T5 | `query UPDATE users SET ...` | | raises ToolError 'sql_guard' |
| T6 | `query` exceeds rate limit | rate=2/min, burst=2, call 3x | 3rd raises ToolError 'rate_limit_exceeded' |
| T7 | `query SELECT * FROM users` no LIMIT | max_rows=100 | audit log shows SQL với LIMIT 100 injected |
| T8 | `query` DB error (bad SQL that passes guard) | `SELECT * FROM nonexistent` | ToolError 'db_error', audit status=failed |
| T9 | `explain SELECT 1` | SQLite | plan returned (non-empty) |
| T10 | `explain UPDATE ...` | | ToolError sql_guard |

4. **Test pattern — fixture SQLite**:
   ```python
   import pytest
   from sqlalchemy import create_engine, text
   from dbread.config import Settings, ConnectionConfig, AuditConfig
   from dbread.connections import ConnectionManager
   from dbread.sql_guard import SqlGuard
   from dbread.rate_limiter import RateLimiter
   from dbread.audit import AuditLogger
   from dbread.tools import ToolHandlers, ToolError

   @pytest.fixture
   def handlers(tmp_path):
       db = tmp_path / "test.db"
       eng = create_engine(f"sqlite:///{db}")
       with eng.connect() as c:
           c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)"))
           c.execute(text("INSERT INTO users VALUES (1,'a'),(2,'b'),(3,'c')"))
           c.commit()
       settings = Settings(
           connections={"test": ConnectionConfig(url=f"sqlite:///{db}", dialect="sqlite", rate_limit_per_min=60, max_rows=100)},
           audit=AuditConfig(path=str(tmp_path/"audit.jsonl"), rotate_mb=1),
       )
       cm = ConnectionManager(settings)
       return ToolHandlers(settings, cm, SqlGuard(), RateLimiter(settings), AuditLogger(settings.audit.path, 1))

   def test_list_connections(handlers):
       assert handlers.list_connections() == [{"name": "test", "dialect": "sqlite"}]

   def test_query_ok(handlers):
       r = handlers.query("test", "SELECT * FROM users")
       assert r["row_count"] == 3

   def test_query_rejects_update(handlers):
       with pytest.raises(ToolError, match="sql_guard"):
           handlers.query("test", "UPDATE users SET name='x'")
   ```

5. **Manual smoke test** — add to Phase 06 (Claude Code MCP config) but verify now:
   - `uv run dbread` → stays alive, reads stdin
   - Send MCP initialize message manually → get response (can skip if E2E tested Phase 07)

6. **Run tests** — `uv run pytest tests/test_tools.py -v`.

## Todo List

- [ ] Implement `src/dbread/tools.py` — `ToolHandlers` with 5 methods
- [ ] Implement `src/dbread/server.py` — MCP Server + stdio transport + tool schemas
- [ ] Verify `uv run dbread` starts without error (will hang waiting stdin — Ctrl+C OK)
- [ ] Write `tests/test_tools.py` — fixture + 10 cases
- [ ] Verify list/describe/query happy path (T1-T4)
- [ ] Verify guard reject path (T5, T10)
- [ ] Verify rate limit path (T6)
- [ ] Verify LIMIT inject verified via audit (T7)
- [ ] Verify DB error path (T8)
- [ ] Verify explain (T9)
- [ ] `uv run pytest` all phases green
- [ ] Each file < 200 LOC

## Success Criteria

- 5 tools callable qua MCP — list_tools trả đúng schema
- Happy path `query` trả rows JSON
- Guard reject path logs audit with reason
- Rate limit blocks 3rd over-limit call
- DB error logs audit status=failed, re-raises as ToolError
- `describe_table` trả columns + indexes qua SA inspector
- Server startup/shutdown không leak engine

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| MCP SDK API breaking change | Low | High | Pin version Phase 01; test manual sau upgrade |
| `inspect()` không work cho Oracle/MSSQL system schemas | Low | Medium | User truyền `schema` param; docs note; fall back raw query `information_schema` (future) |
| EXPLAIN syntax khác biệt dialect | Medium | Low | `_build_explain` cover 5 dialect; default fallback `EXPLAIN` |
| Stdout accidentally used (print) → break MCP protocol | Medium | High | Logging config qua `stream=sys.stderr`; code review no `print(` |
| Huge result set blow memory | Low | Medium | `fetchmany(max_rows)` bounded; max_rows ≤ cfg.max_rows cap |

## Security Considerations

- **Error message leak:** `db_error: <str(e)>[:200]` — có thể chứa SQL/data. Truncate 200 chars, docs note.
- **Stderr logs:** cẩn thận không log password (engine URL) — SA default mask, nhưng verify `log.exception` khi error không re-throw DSN
- **AI prompt injection in SQL:** AI có thể encode DROP trong string literal → parser sees literal, passes; DB user Layer 0 vẫn block (e.g. `SELECT 'DROP TABLE x'` là SELECT hợp lệ, không chạy DROP)
- **Schema info leak:** `list_tables` + `describe_table` expose schema info → intended, AI cần để query. If sensitive, user giới hạn DB user chỉ GRANT tables cần thiết.

## Next Steps

- **Blocks:** Phase 07 (integration tests needs MCP tools)
- **Dependencies:** Phase 02 (config, connections, audit), Phase 03 (guard), Phase 04 (rate limiter) — all required
- **Follow-up:** Phase 06 docs parallel; Phase 07 integration last
