"""SQL Guard test matrix - the critical security boundary.

35+ parametrized cases covering happy path, DML/DDL/DCL reject,
multi-statement, CTE-DML evasion, function blacklist, case/comment
evasion, INTO variants, unknown commands, and LIMIT injection.
"""

from __future__ import annotations

import pytest

from dbread.sql_guard import FUNCTION_BLACKLIST, SqlGuard

guard = SqlGuard()


ALLOW_CASES = [
    ("SELECT 1", "postgres"),
    ("SELECT * FROM users WHERE id = 1", "postgres"),
    ("SELECT u.id, o.total FROM users u JOIN orders o ON u.id = o.uid", "postgres"),
    ("SELECT 1 UNION SELECT 2", "postgres"),
    ("WITH t AS (SELECT 1) SELECT * FROM t", "postgres"),
    ("EXPLAIN SELECT * FROM users", "postgres"),
    ("SHOW TABLES", "mysql"),
    ("DESCRIBE users", "mysql"),
    ("DESC users", "mysql"),
    ("SELECT * FROM users LIMIT 10", "postgres"),
    ("SELECT COUNT(*) FROM orders GROUP BY status", "postgres"),
]


REJECT_CASES = [
    # DML
    ("INSERT INTO users VALUES (1)", "postgres", "Insert"),
    ("UPDATE users SET x = 1", "postgres", "Update"),
    ("DELETE FROM users", "postgres", "Delete"),
    ("MERGE INTO t USING s ON s.id=t.id WHEN MATCHED THEN UPDATE SET x=1", "postgres", "Merge"),
    # DDL
    ("CREATE TABLE x (id int)", "postgres", "Create"),
    ("ALTER TABLE x ADD COLUMN y int", "postgres", "Alter"),
    ("DROP TABLE x", "postgres", "Drop"),
    ("TRUNCATE TABLE x", "postgres", "Truncate"),
    # DCL
    ("GRANT SELECT ON x TO y", "postgres", "Grant"),
    ("REVOKE SELECT ON x FROM y", "postgres", "revoke"),
    # Multi-statement
    ("SELECT 1; DROP TABLE x", "postgres", "multi_statement"),
    # CTE-DML evasion (PostgreSQL trick)
    ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "postgres", "Delete"),
    ("WITH u AS (UPDATE t SET x=1 RETURNING *) SELECT * FROM u", "postgres", "Update"),
    ("WITH i AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM i", "postgres", "Insert"),
    # Comment + case evasion
    ("/* ignore */ DELETE FROM users", "postgres", "Delete"),
    ("DeLeTe FrOm users", "postgres", "Delete"),
    # INTO variants
    ("SELECT * INTO newtbl FROM users", "postgres", "Into"),
    # Unknown / server commands
    ("VACUUM users", "postgres", "not_allowed"),
    ("CALL do_stuff()", "postgres", "not_allowed"),
    # Fail closed
    ("invalid sql !!!", "postgres", "parse_error"),
    ("", "postgres", "empty"),
    ("   ", "postgres", "empty"),
]


FUNCTION_REJECT_CASES = [
    ("SELECT pg_read_file('/etc/passwd')", "postgres", "pg_read_file"),
    ("SELECT pg_advisory_lock(1)", "postgres", "pg_advisory_lock"),
    ("SELECT xp_cmdshell('dir')", "tsql", "xp_cmdshell"),
    ("SELECT LOAD_FILE('/etc/passwd')", "mysql", "load_file"),
    ("SELECT sleep(10)", "mysql", "sleep"),
    ("SELECT dblink_exec('...')", "postgres", "dblink_exec"),
    # v0.2 additions - time-based DoS
    ("SELECT pg_sleep(5)", "postgres", "pg_sleep"),
    ("SELECT pg_sleep_for('5 s')", "postgres", "pg_sleep_for"),
    # dbms_lock.sleep parses with name='sleep' - caught by existing 'sleep'.
    ("SELECT dbms_lock.sleep(1) FROM dual", "oracle", "sleep"),
    ("SELECT dbms_session.sleep(1) FROM dual", "oracle", "sleep"),
]


WAITFOR_CASES = [
    "WAITFOR DELAY '00:00:05'",
    "waitfor  time '23:00'",
    "/* blah */ WAITFOR DELAY '00:00:01'",
    "-- comment\nWAITFOR DELAY '00:00:01'",
]


@pytest.mark.parametrize("sql", WAITFOR_CASES)
def test_reject_waitfor(sql: str) -> None:
    r = guard.validate(sql, "tsql")
    assert not r.allowed, sql
    assert "WAITFOR" in (r.reason or "")


@pytest.mark.parametrize("sql,dialect", ALLOW_CASES)
def test_allow(sql: str, dialect: str) -> None:
    r = guard.validate(sql, dialect)
    assert r.allowed, f"should allow {sql!r} on {dialect} but rejected: {r.reason}"


@pytest.mark.parametrize("sql,dialect,fragment", REJECT_CASES)
def test_reject(sql: str, dialect: str, fragment: str) -> None:
    r = guard.validate(sql, dialect)
    assert not r.allowed, f"should reject {sql!r} but allowed"
    assert r.reason is not None
    assert fragment.lower() in r.reason.lower(), (
        f"expected fragment {fragment!r} in reason but got: {r.reason!r}"
    )


@pytest.mark.parametrize("sql,dialect,func", FUNCTION_REJECT_CASES)
def test_reject_blacklisted_function(sql: str, dialect: str, func: str) -> None:
    r = guard.validate(sql, dialect)
    assert not r.allowed
    assert r.reason is not None
    assert func in r.reason.lower(), f"expected {func} in {r.reason!r}"


def test_function_blacklist_has_key_items() -> None:
    critical = {"pg_read_file", "xp_cmdshell", "load_file", "dblink_exec"}
    assert critical.issubset(FUNCTION_BLACKLIST)


def test_limit_inject_basic() -> None:
    out = guard.inject_limit("SELECT * FROM users", "postgres", 100)
    assert "LIMIT 100" in out.upper()


def test_limit_preserve_existing() -> None:
    out = guard.inject_limit("SELECT * FROM users LIMIT 5", "postgres", 100)
    assert "LIMIT 5" in out.upper()
    assert "LIMIT 100" not in out.upper()


def test_limit_with_cte() -> None:
    sql = "WITH t AS (SELECT 1) SELECT * FROM t"
    out = guard.inject_limit(sql, "postgres", 100)
    assert "LIMIT 100" in out.upper()


def test_limit_with_union() -> None:
    sql = "SELECT 1 UNION SELECT 2"
    out = guard.inject_limit(sql, "postgres", 100)
    assert "LIMIT 100" in out.upper()


def test_limit_no_crash_on_parse_error() -> None:
    out = guard.inject_limit("not sql !!!", "postgres", 100)
    assert out == "not sql !!!"


def test_limit_no_change_for_show() -> None:
    sql = "SHOW TABLES"
    out = guard.inject_limit(sql, "mysql", 100)
    assert "LIMIT" not in out.upper()


def test_ast_returned_on_allow() -> None:
    r = guard.validate("SELECT 1", "postgres")
    assert r.allowed
    assert r.ast is not None


def test_ast_none_on_reject() -> None:
    r = guard.validate("DELETE FROM x", "postgres")
    assert not r.allowed
    assert r.ast is None
