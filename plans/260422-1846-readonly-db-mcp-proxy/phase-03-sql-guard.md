---
phase: 03
title: "SQL Guard — sqlglot AST validation (CRITICAL)"
status: pending
priority: P0
effort: 5h
created: 2026-04-22
---

# Phase 03 — SQL Guard (CRITICAL PATH)

## Context Links

- Brainstorm §4.6 SQL Guard Rules, §6 Risks: [../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md](../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md)
- Previous: [phase-02-core-foundation.md](phase-02-core-foundation.md)
- sqlglot docs: https://sqlglot.com/

## Overview

- **Priority:** P0 — THE critical security component. Layer 1 of defense in depth.
- **Status:** pending
- **Description:** Implement `SqlGuard` class: parse SQL bằng sqlglot AST, reject mọi DML/DDL/DCL/multi-statement/side-effect function/CTE-with-DML, auto-inject LIMIT cho top-level SELECT. Ship với 20+ test cases cover evasion patterns.

## Key Insights

- sqlglot parse multi-dialect: PG, MySQL, MSSQL, SQLite, Oracle, Snowflake, BigQuery, ... → bao quát đủ
- AST node types quan trọng: `exp.Select`, `exp.Insert`, `exp.Update`, `exp.Delete`, `exp.Merge`, `exp.Create`, `exp.Drop`, `exp.Alter`, `exp.TruncateTable`, `exp.Command` (catch-all unknown), `exp.With` (CTE)
- **PG CTE trick**: `WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d` — `exp.With` top-level nhưng inner có `exp.Delete` → phải walk vào `With.args['expressions']` (CTE list) + mỗi CTE `this` có thể là DML
- **Function side effects**: PG `lo_import`, `lo_export`, `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `dblink_exec`, `pg_advisory_lock*`; MSSQL `xp_cmdshell`, `xp_regread`, `sp_*` (thận trọng); MySQL `LOAD_FILE`, `INTO OUTFILE` (handle ở parser syntax level — xuất hiện dưới `exp.Into`/`exp.LoadData`)
- **Multi-statement**: sqlglot `parse(sql)` returns `list[Expression]` — len > 1 → reject
- **Comment stripping**: sqlglot handles comments natively — `/* DROP */ SELECT 1` parse đúng là SELECT, comment-in-keyword evasion handled
- **Case insensitive**: sqlglot normalize — `DeLeTe` vẫn phát hiện
- **Unknown/unparseable**: raise `sqlglot.errors.ParseError` → reject (fail-closed)
- **`Command` node**: sqlglot dùng cho statement không biết (e.g. `VACUUM`, custom) → default reject
- LIMIT injection: dùng `exp.Select.limit()` method của sqlglot → safe render lại

## Requirements

**Functional:**
- `validate(sql, dialect) -> GuardResult` trả về `{allowed: bool, reason: str | None, ast: Expression | None}`
- `inject_limit(sql, dialect, max_rows) -> str` — inject LIMIT nếu top-level SELECT thiếu, idempotent nếu đã có
- Support dialect: `postgres`, `mysql`, `tsql` (MSSQL), `sqlite`, `oracle`

**Non-functional:**
- Validate call < 10ms typical query
- Zero false-positive cho valid SELECT/EXPLAIN/SHOW/DESCRIBE
- Fail-closed: parse error → reject (log reason)
- File < 200 LOC

## Architecture

```
SqlGuard
├── REJECT_NODES: set[type[Expression]]  # Insert, Update, Delete, Merge, Create, Drop, Alter, TruncateTable, Command...
├── ALLOW_TOP_LEVEL: set[type[Expression]]  # Select, With, Describe, Show, Explain, Use (read-only)
├── FUNCTION_BLACKLIST: set[str]         # pg_read_file, lo_import, dblink_exec, xp_cmdshell, ...
├── validate(sql, dialect) → GuardResult
│       1. sqlglot.parse(sql, read=dialect)  → list[Expression]
│       2. if len != 1: reject "multi-statement"
│       3. root = stmts[0]
│       4. if type(root) not in ALLOW_TOP_LEVEL: reject "type X not allowed"
│       5. walk root.walk():
│             - if type(node) in REJECT_NODES: reject
│             - if isinstance(node, (exp.Anonymous, exp.Func)): check name lowercase in FUNCTION_BLACKLIST
│             - if isinstance(node, exp.Into): reject (INTO OUTFILE / SELECT INTO TABLE)
│       6. if root is With: walk CTEs, each CTE.this must pass same DML checks
│       7. return allowed
│
└── inject_limit(sql, dialect, max_rows) → str
        1. parse → Select
        2. if isinstance(root, Select) and not root.args.get('limit'):
             root = root.limit(max_rows)
        3. return root.sql(dialect=dialect)
```

## Related Code Files

**Create:**
- `src/dbread/sql_guard.py` (~150 LOC)
- `tests/test_sql_guard.py` (20+ cases, comprehensive)

**Modify:**
- none

## Implementation Steps

1. **Define constants** tại top của `sql_guard.py`:
   ```python
   import sqlglot
   from sqlglot import exp
   from dataclasses import dataclass

   REJECT_NODES = (
       exp.Insert, exp.Update, exp.Delete, exp.Merge,
       exp.Create, exp.Drop, exp.Alter, exp.TruncateTable,
       exp.Command,  # catch-all unknown (VACUUM, REINDEX, etc)
   )

   ALLOW_TOP_LEVEL = (
       exp.Select, exp.With, exp.Describe, exp.Show, exp.Use,
       # Explain wraps inner — checked separately below
   )

   FUNCTION_BLACKLIST = {
       # PostgreSQL
       'pg_read_file', 'pg_read_binary_file', 'pg_ls_dir', 'pg_stat_file',
       'lo_import', 'lo_export', 'lo_from_bytea', 'lo_put', 'lo_unlink',
       'dblink_exec', 'dblink_send_query',
       'pg_advisory_lock', 'pg_advisory_lock_shared',
       'pg_advisory_xact_lock', 'pg_try_advisory_lock',
       'pg_terminate_backend', 'pg_cancel_backend',
       'pg_reload_conf', 'pg_rotate_logfile',
       # MSSQL
       'xp_cmdshell', 'xp_regread', 'xp_regwrite', 'xp_dirtree',
       'sp_oacreate', 'sp_oamethod', 'sp_configure',
       # MySQL
       'load_file', 'sleep', 'benchmark',
       # Oracle
       'dbms_xmlgen', 'utl_file', 'utl_http',
   }
   ```

2. **`GuardResult` dataclass**:
   ```python
   @dataclass
   class GuardResult:
       allowed: bool
       reason: str | None = None
       ast: exp.Expression | None = None
   ```

3. **`validate` method**:
   ```python
   class SqlGuard:
       def validate(self, sql: str, dialect: str) -> GuardResult:
           try:
               stmts = sqlglot.parse(sql, read=dialect)
           except sqlglot.errors.ParseError as e:
               return GuardResult(False, f"parse_error: {e}")
           stmts = [s for s in stmts if s is not None]
           if len(stmts) == 0:
               return GuardResult(False, "empty_sql")
           if len(stmts) > 1:
               return GuardResult(False, "multi_statement_not_allowed")

           root = stmts[0]
           # Unwrap Explain
           target = root.args.get('this') if isinstance(root, exp.Command) and root.name.upper() == 'EXPLAIN' else root
           # Explain in sqlglot is exp.Command with name="EXPLAIN" on some dialects, else separate
           if isinstance(root, exp.Expression) and root.key == 'describe':
               pass  # describe is OK
           if not isinstance(root, ALLOW_TOP_LEVEL):
               # Accept EXPLAIN wrapping Select
               if root.key == 'command' and getattr(root, 'name', '').upper() in ('EXPLAIN', 'SHOW', 'DESCRIBE', 'DESC'):
                   pass
               else:
                   return GuardResult(False, f"top_level_not_allowed: {type(root).__name__}")

           # Walk AST
           for node in root.walk():
               if isinstance(node, REJECT_NODES):
                   return GuardResult(False, f"node_rejected: {type(node).__name__}")
               if isinstance(node, exp.Into):
                   return GuardResult(False, "select_into_not_allowed")
               if isinstance(node, (exp.Anonymous, exp.Func)):
                   name = (node.name or '').lower()
                   if name in FUNCTION_BLACKLIST:
                       return GuardResult(False, f"function_blacklisted: {name}")

           return GuardResult(True, ast=root)
   ```

4. **`inject_limit` method**:
   ```python
       def inject_limit(self, sql: str, dialect: str, max_rows: int) -> str:
           stmts = sqlglot.parse(sql, read=dialect)
           if len(stmts) != 1:
               return sql  # validate should have caught, return as-is
           root = stmts[0]
           if isinstance(root, exp.Select) and not root.args.get('limit'):
               root = root.limit(max_rows)
           elif isinstance(root, exp.With):
               inner = root.this
               if isinstance(inner, exp.Select) and not inner.args.get('limit'):
                   inner.limit(max_rows, copy=False)
           return root.sql(dialect=dialect)
   ```

5. **Test matrix — `tests/test_sql_guard.py`** (20+ cases). Use `pytest.mark.parametrize` cho groups:

### Test Case Matrix

| # | SQL | Dialect | Expected allowed | Expected reason contains |
|---|-----|---------|-----------------|--------------------------|
| 1 | `SELECT 1` | postgres | True | - |
| 2 | `SELECT * FROM users WHERE id = 1` | postgres | True | - |
| 3 | `SELECT u.id, o.total FROM users u JOIN orders o ON u.id = o.uid` | postgres | True | - |
| 4 | `SELECT 1 UNION SELECT 2` | postgres | True | - |
| 5 | `WITH t AS (SELECT 1) SELECT * FROM t` | postgres | True | - |
| 6 | `EXPLAIN SELECT * FROM users` | postgres | True | - |
| 7 | `SHOW TABLES` | mysql | True | - |
| 8 | `DESCRIBE users` | mysql | True | - |
| 9 | `INSERT INTO users VALUES (1)` | postgres | False | `Insert` |
| 10 | `UPDATE users SET x = 1` | postgres | False | `Update` |
| 11 | `DELETE FROM users` | postgres | False | `Delete` |
| 12 | `MERGE INTO t USING s ON ...` | postgres | False | `Merge` |
| 13 | `CREATE TABLE x (id int)` | postgres | False | `Create` |
| 14 | `ALTER TABLE x ADD COLUMN y int` | postgres | False | `Alter` |
| 15 | `DROP TABLE x` | postgres | False | `Drop` |
| 16 | `TRUNCATE TABLE x` | postgres | False | `Truncate` |
| 17 | `GRANT SELECT ON x TO y` | postgres | False | parse/Command |
| 18 | `REVOKE SELECT ON x FROM y` | postgres | False | parse/Command |
| 19 | `CALL do_stuff()` | postgres | False | Command/Call |
| 20 | `SELECT 1; DROP TABLE x` | postgres | False | `multi_statement` |
| 21 | `WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d` | postgres | False | `Delete` (CTE-DML) |
| 22 | `WITH u AS (UPDATE t SET x=1 RETURNING *) SELECT * FROM u` | postgres | False | `Update` (CTE-DML) |
| 23 | `WITH i AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM i` | postgres | False | `Insert` (CTE-DML) |
| 24 | `SELECT pg_read_file('/etc/passwd')` | postgres | False | `function_blacklisted: pg_read_file` |
| 25 | `SELECT pg_advisory_lock(1)` | postgres | False | `function_blacklisted` |
| 26 | `SELECT xp_cmdshell('dir')` | tsql | False | `function_blacklisted` |
| 27 | `SELECT LOAD_FILE('/etc/passwd')` | mysql | False | `function_blacklisted: load_file` |
| 28 | `/* ignore */ DELETE FROM users` | postgres | False | `Delete` (comment ignored) |
| 29 | `DeLeTe FrOm users` | postgres | False | `Delete` (case insensitive) |
| 30 | `SELECT * INTO newtbl FROM users` | postgres | False | `select_into_not_allowed` |
| 31 | `SELECT * FROM users INTO OUTFILE '/tmp/x'` | mysql | False | `select_into` / `Into` |
| 32 | `VACUUM users` | postgres | False | `Command` |
| 33 | `invalid sql !!!` | postgres | False | `parse_error` |
| 34 | `` (empty) | postgres | False | `empty_sql` |
| 35 | `SELECT * FROM users LIMIT 10` | postgres | True | - (pre-existing LIMIT OK) |

### LIMIT Injection Tests

| # | SQL in | max_rows | Expected SQL out |
|---|--------|----------|------------------|
| L1 | `SELECT * FROM users` | 100 | contains `LIMIT 100` |
| L2 | `SELECT * FROM users LIMIT 5` | 100 | contains `LIMIT 5` (no override) |
| L3 | `WITH t AS (SELECT 1) SELECT * FROM t` | 100 | outer SELECT gets `LIMIT 100` |
| L4 | `SELECT 1 UNION SELECT 2` | 100 | outer UNION gets `LIMIT 100` (or doc as known limitation) |

6. **Test implementation pattern**:
   ```python
   import pytest
   from dbread.sql_guard import SqlGuard

   guard = SqlGuard()

   ALLOW_CASES = [
       ("SELECT 1", "postgres"),
       ("SELECT * FROM users WHERE id=1", "postgres"),
       # ...
   ]

   REJECT_CASES = [
       ("INSERT INTO t VALUES (1)", "postgres", "Insert"),
       ("SELECT 1; DROP TABLE x", "postgres", "multi_statement"),
       ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "postgres", "Delete"),
       # ...
   ]

   @pytest.mark.parametrize("sql,dialect", ALLOW_CASES)
   def test_allow(sql, dialect):
       r = guard.validate(sql, dialect)
       assert r.allowed, f"should allow but rejected: {r.reason}"

   @pytest.mark.parametrize("sql,dialect,reason_frag", REJECT_CASES)
   def test_reject(sql, dialect, reason_frag):
       r = guard.validate(sql, dialect)
       assert not r.allowed
       assert reason_frag.lower() in r.reason.lower()

   def test_limit_inject():
       out = guard.inject_limit("SELECT * FROM users", "postgres", 100)
       assert "LIMIT 100" in out.upper()

   def test_limit_preserve_existing():
       out = guard.inject_limit("SELECT * FROM users LIMIT 5", "postgres", 100)
       assert "LIMIT 5" in out.upper()
       assert "LIMIT 100" not in out.upper()
   ```

7. **Run tests** — `uv run pytest tests/test_sql_guard.py -v` → all pass. Any fail → refine AST walk logic.

8. **Coverage gate** — `uv run pytest --cov=dbread.sql_guard --cov-fail-under=90`.

## Todo List

- [ ] Implement `src/dbread/sql_guard.py` — constants + `GuardResult` + `SqlGuard.validate` + `SqlGuard.inject_limit`
- [ ] Write `tests/test_sql_guard.py` with 35+ cases parametrized
- [ ] Verify happy-path cases pass (cases 1-8, 35)
- [ ] Verify DML/DDL reject (9-19)
- [ ] Verify multi-statement reject (20)
- [ ] Verify CTE-DML evasion reject (21-23) — **critical PG trick**
- [ ] Verify function blacklist (24-27)
- [ ] Verify comment + case evasion (28-29)
- [ ] Verify INTO variants (30-31)
- [ ] Verify unknown command reject (32)
- [ ] Verify parse error fail-closed (33-34)
- [ ] Verify LIMIT inject behavior (L1-L4)
- [ ] `uv run pytest --cov=dbread.sql_guard` ≥ 90%
- [ ] File < 200 LOC

## Success Criteria

- Tất cả 35+ test cases PASS
- Coverage ≥ 90% cho `sql_guard.py`
- Zero false-positive trên valid SELECT set
- CTE-DML evasion (test 21-23) blocked — **absolute must**
- Function blacklist catch cả PG + MSSQL + MySQL patterns
- Parse error → reject (fail-closed), không crash

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| sqlglot node type rename across version | Low | Medium | Pin `sqlglot>=23.0,<25.0`, catch AttributeError in walk |
| Dialect-specific syntax không parse | Medium | Medium | Fail-closed (parse_error → reject); user can set correct dialect in config |
| Function blacklist incomplete (new exploit) | Medium | Medium | Layer 0 (DB user permission) là net cuối; blacklist update via docs |
| LIMIT inject breaks UNION semantics | Low | Low | For UNION, wrap or skip (doc as known limitation) |
| Query vẫn có side effect qua volatile function không blacklist | Low | Low | Layer 0 + `default_transaction_read_only` (docs) |
| sqlglot bug trên Oracle/MSSQL dialect | Medium | Medium | Integration test (Phase 07) sẽ expose, fall back `parse_error` reject |

## Security Considerations

- **Fail-closed default:** bất kỳ exception parse → reject. Never "allow on unknown".
- **Layer 0 is the real guarantee:** guard là belt, DB user read-only là suspenders. Docs phải emphasize Layer 0 non-negotiable.
- **Function blacklist:** blocklist not allowlist → new function có thể bypass. Mitigation: DB user không có EXECUTE trên superuser functions (e.g. PG `pg_read_file` requires superuser).
- **Audit integration:** mọi reject phải log reason qua audit (Phase 05 wire).
- **Dialect mismatch:** user config `dialect: postgres` nhưng gọi MySQL-specific syntax → parse error → reject (safe).

## Next Steps

- **Blocks:** Phase 05 (MCP Tools wire guard vào `query` và `explain`)
- **Dependencies:** Phase 02 (không direct dependency nhưng cùng module tree)
- **Follow-up:** Phase 04 (Rate Limiter) có thể parallel
