"""Adversarial / fuzz cases for SqlGuard.

Every case MUST be rejected. Some will be blocked by Layer 1 (AST or
function blacklist); others by parse_error from the dialect-specific
parser (fail-closed). If any case slips through, either the guard must
be patched OR the case must be moved to `EXPECTED_L0_ONLY` with an
explicit doc-trail in docs/security-threat-model.md.

Categories (10):
    1. ClickHouse system functions
    2. Oracle PL/SQL blocks
    3. MSSQL batch tricks
    4. PG advisory-lock / admin DoS
    5. MySQL writes disguised as SELECT
    6. DuckDB file I/O
    7. SQLite attach / PRAGMA
    8. Comment-smuggle
    9. Nested CTE-DML
    10. SET / session mutations
"""

from __future__ import annotations

import pytest

from dbread.sql_guard import SqlGuard

guard = SqlGuard()


# (dialect, sql) — all MUST reject for any reason.
ADVERSARIAL_CASES: list[tuple[str, str]] = [
    # 1. ClickHouse system functions / external readers
    ("clickhouse", "SELECT file('/etc/passwd')"),
    ("clickhouse", "SELECT url('http://evil/x', 'CSV')"),
    ("clickhouse", "SELECT s3('http://evil/bucket/x', 'CSV')"),
    ("clickhouse", "SELECT remote('other-host:9000', 'system.tables')"),
    ("clickhouse", "SELECT remoteSecure('evil:9440', 'db.t')"),
    ("clickhouse", "SELECT * FROM system.one; DROP TABLE x"),
    ("clickhouse", "SELECT cluster('c','system','tables')"),
    ("clickhouse", "SELECT hdfs('hdfs://evil/x', 'CSV')"),
    # 2. Oracle PL/SQL blocks
    ("oracle", "BEGIN DELETE FROM t; END;"),
    ("oracle", "DECLARE v NUMBER; BEGIN v := 1; END;"),
    ("oracle", "CALL my_proc()"),
    ("oracle", "EXEC dbms_utility.exec_ddl_statement('DROP TABLE t')"),
    # 3. MSSQL batch tricks
    ("tsql", "; WAITFOR DELAY '0:0:30'"),
    ("tsql", "EXEC sp_configure 'show advanced options', 1"),
    ("tsql", "BACKUP DATABASE mydb TO DISK='/tmp/x.bak'"),
    ("tsql", "BULK INSERT t FROM 'c:/x.csv'"),
    ("tsql", "EXEC('DELETE FROM t')"),
    # 4. PG advisory-lock / admin DoS
    ("postgres", "SELECT pg_advisory_lock(1)"),
    ("postgres", "SELECT pg_sleep_for('1 hour')"),
    ("postgres", "SELECT pg_terminate_backend(12345)"),
    ("postgres", "SELECT pg_cancel_backend(12345)"),
    ("postgres", "SELECT pg_reload_conf()"),
    # 5. MySQL writes disguised
    ("mysql", "SELECT * FROM users INTO OUTFILE '/tmp/x'"),
    ("mysql", "SELECT LOAD_FILE('/etc/passwd')"),
    ("mysql", "SELECT SLEEP(100)"),
    ("mysql", "SELECT BENCHMARK(100000, MD5('x'))"),
    ("mysql", "HANDLER users OPEN"),
    # 6. DuckDB file / network I/O
    ("duckdb", "COPY t TO '/tmp/x.csv'"),
    ("duckdb", "INSTALL httpfs"),
    ("duckdb", "LOAD httpfs"),
    ("duckdb", "ATTACH 'http://evil/x.db'"),
    ("duckdb", "SELECT * FROM read_csv('http://evil/x.csv')"),
    ("duckdb", "SELECT * FROM read_parquet('s3://evil/x.parquet')"),
    # 7. SQLite attach / PRAGMA
    ("sqlite", "ATTACH DATABASE '/tmp/x.db' AS y"),
    ("sqlite", "PRAGMA writable_schema = 1"),
    ("sqlite", "VACUUM INTO '/tmp/x.db'"),
    # 8. Comment-smuggle (trailing statement after end-of-line/block comment)
    ("mysql", "SELECT 1--\nDROP TABLE x"),
    ("postgres", "SELECT 1; /* harmless */ DELETE FROM t"),
    # 9. Nested CTE-DML
    (
        "postgres",
        "WITH a AS (SELECT 1), b AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM b",
    ),
    (
        "postgres",
        "WITH x AS (UPDATE t SET a=1 RETURNING *), y AS (SELECT * FROM x) SELECT * FROM y",
    ),
    # 10. SET / session mutations
    ("postgres", "SET ROLE superuser"),
    ("postgres", "SET SESSION AUTHORIZATION postgres"),
    ("postgres", "RESET ALL"),
    ("mysql", "SET GLOBAL general_log = 'ON'"),
]


@pytest.mark.parametrize("dialect,sql", ADVERSARIAL_CASES)
def test_adversarial_rejected(dialect: str, sql: str) -> None:
    r = guard.validate(sql, dialect)
    assert not r.allowed, (
        f"ADVERSARIAL CASE LEAKED: dialect={dialect} sql={sql!r} — "
        f"guard returned allowed=True. Patch sql_guard or doc as L0-only."
    )
    assert r.reason, "rejection must include reason"
