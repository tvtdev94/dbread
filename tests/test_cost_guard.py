"""Unit tests for the pre-execution cost guard.

Covers per-dialect parsers (PG, MySQL), fail-open semantics, and integration
with ``tools.query()``. Uses fake SQLAlchemy-shaped engines instead of Docker
so the suite stays unit-tier.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from dbread.audit import AuditLogger
from dbread.config import AuditConfig, ConnectionConfig, Settings
from dbread.connections import ConnectionManager
from dbread.cost_guard import CostGuard
from dbread.rate_limiter import RateLimiter
from dbread.sql_guard import SqlGuard
from dbread.tools import ToolError, ToolHandlers

# ---------------------------------------------------------------------------
# Fake engine plumbing — quacks like SQLAlchemy enough for the parsers.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row: tuple | None) -> None:
        self._row = row

    def fetchone(self) -> tuple | None:
        return self._row


class _FakeConn:
    def __init__(self, payload: Any | Exception) -> None:
        self._payload = payload
        self.executed: list[str] = []

    def execute(self, stmt: Any) -> _FakeResult:
        self.executed.append(str(stmt))
        if isinstance(self._payload, Exception):
            raise self._payload
        return _FakeResult((self._payload,) if self._payload is not None else None)


class _FakeEngine:
    def __init__(self, payload: Any | Exception) -> None:
        self.payload = payload
        self.last_conn: _FakeConn | None = None

    @contextmanager
    def connect(self):  # noqa: ANN201 — context manager, mimics SQLAlchemy
        self.last_conn = _FakeConn(self.payload)
        try:
            yield self.last_conn
        finally:
            pass


# ---------------------------------------------------------------------------
# Parser-level tests
# ---------------------------------------------------------------------------


def test_pg_parser_extracts_plan_rows() -> None:
    pg_plan = json.dumps(
        [{"Plan": {"Node Type": "Seq Scan", "Plan Rows": 4242, "Total Cost": 100.0}}]
    )
    engine = _FakeEngine(pg_plan)
    cg = CostGuard()
    rows, ms = cg.check("SELECT * FROM users", "postgres", engine, threshold=1000)
    assert rows == 4242
    assert ms >= 0
    assert "EXPLAIN (FORMAT JSON)" in engine.last_conn.executed[0]


def test_pg_parser_accepts_already_parsed_json() -> None:
    # psycopg3 / asyncpg may return list/dict directly without str round-trip
    payload = [{"Plan": {"Plan Rows": 7}}]
    rows, _ = CostGuard().check("SELECT 1", "postgres", _FakeEngine(payload), threshold=1)
    assert rows == 7


def test_pg_parser_missing_plan_returns_none() -> None:
    bad = json.dumps([{"NotAPlan": {}}])
    rows, _ = CostGuard().check("SELECT 1", "postgres", _FakeEngine(bad), threshold=1)
    assert rows is None


def test_mysql_parser_flat_query_block() -> None:
    mysql_plan = json.dumps(
        {
            "query_block": {
                "select_id": 1,
                "table": {"table_name": "users", "rows_examined_per_scan": 1500},
            }
        }
    )
    rows, _ = CostGuard().check("SELECT 1", "mysql", _FakeEngine(mysql_plan), threshold=10)
    assert rows == 1500


def test_mysql_parser_nested_joins_sums_rows() -> None:
    mysql_plan = json.dumps(
        {
            "query_block": {
                "nested_loop": [
                    {"table": {"table_name": "a", "rows_examined_per_scan": 100}},
                    {"table": {"table_name": "b", "rows_examined_per_scan": 250}},
                ]
            }
        }
    )
    rows, _ = CostGuard().check("SELECT 1", "mysql", _FakeEngine(mysql_plan), threshold=10)
    assert rows == 350


def test_mysql_parser_zero_rows_treated_as_unknown() -> None:
    mysql_plan = json.dumps({"query_block": {"select_id": 1}})
    rows, _ = CostGuard().check("SELECT 1", "mysql", _FakeEngine(mysql_plan), threshold=10)
    assert rows is None


def test_mysql_parser_skips_nested_query_block_subquery() -> None:
    # materialized_from_subquery contains its own query_block — must NOT
    # double-count rows already represented by the parent's row scan.
    mysql_plan = json.dumps(
        {
            "query_block": {
                "table": {
                    "table_name": "u",
                    "rows_examined_per_scan": 100,
                    "materialized_from_subquery": {
                        "query_block": {
                            "table": {"table_name": "inner", "rows_examined_per_scan": 9999},
                        }
                    },
                }
            }
        }
    )
    rows, _ = CostGuard().check("SELECT 1", "mysql", _FakeEngine(mysql_plan), threshold=10)
    assert rows == 100  # not 100 + 9999


def test_mysql_parser_skips_attached_subqueries() -> None:
    # attached_subqueries: list of {query_block: ...} — also must be skipped.
    mysql_plan = json.dumps(
        {
            "query_block": {
                "table": {"table_name": "u", "rows_examined_per_scan": 50},
                "attached_subqueries": [
                    {"dependent": True, "query_block": {
                        "table": {"table_name": "x", "rows_examined_per_scan": 5000},
                    }}
                ],
            }
        }
    )
    rows, _ = CostGuard().check("SELECT 1", "mysql", _FakeEngine(mysql_plan), threshold=10)
    assert rows == 50


def test_unsupported_dialect_returns_none_zero() -> None:
    rows, ms = CostGuard().check("SELECT 1", "sqlite", _FakeEngine(""), threshold=1)
    assert rows is None
    assert ms == 0


def test_threshold_none_short_circuits() -> None:
    engine = _FakeEngine("[{\"Plan\": {\"Plan Rows\": 1}}]")
    rows, ms = CostGuard().check("SELECT 1", "postgres", engine, threshold=None)
    assert (rows, ms) == (None, 0)
    # parser must NOT have been invoked
    assert engine.last_conn is None


def test_db_error_fails_open() -> None:
    engine = _FakeEngine(RuntimeError("planner exploded"))
    rows, ms = CostGuard().check("SELECT 1", "postgres", engine, threshold=1)
    assert rows is None
    assert ms >= 0


def test_pg_parser_non_int_plan_rows_returns_none() -> None:
    bad = json.dumps([{"Plan": {"Plan Rows": "not-an-int"}}])
    rows, _ = CostGuard().check("SELECT 1", "postgres", _FakeEngine(bad), threshold=1)
    assert rows is None


# ---------------------------------------------------------------------------
# Integration with tools.query() — uses SQLite (unsupported dialect ⇒ proceed)
# ---------------------------------------------------------------------------


def _seed_sqlite(db_path: Path) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)"))
        conn.execute(text("INSERT INTO t (id, v) VALUES (1, 'a'), (2, 'b'), (3, 'c')"))
    engine.dispose()


def _build(
    tmp_path: Path,
    *,
    max_rows_estimate: int | None,
    cost_guard: CostGuard | None = None,
) -> tuple[ToolHandlers, Path]:
    db = tmp_path / "t.db"
    _seed_sqlite(db)
    audit_path = tmp_path / "audit.jsonl"
    settings = Settings(
        connections={
            "c": ConnectionConfig(
                url=f"sqlite:///{db}",
                dialect="sqlite",
                max_rows=100,
                max_rows_estimate=max_rows_estimate,
            )
        },
        audit=AuditConfig(path=str(audit_path), rotate_mb=1),
    )
    cm = ConnectionManager(settings)
    h = ToolHandlers(
        settings=settings,
        conn_mgr=cm,
        guard=SqlGuard(),
        rate_limiter=RateLimiter(settings),
        audit=AuditLogger(str(audit_path), 1),
        cost_guard=cost_guard or CostGuard(),
    )
    return h, audit_path


def test_max_rows_estimate_none_keeps_bc(tmp_path: Path) -> None:
    h, audit_path = _build(tmp_path, max_rows_estimate=None)
    out = h.query("c", sql="SELECT * FROM t")
    assert out["row_count"] == 3
    # No cost_check_ms recorded when guard disabled
    rec = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert "cost_check_ms" not in rec


def test_unsupported_dialect_records_zero_cost_ms(tmp_path: Path) -> None:
    h, audit_path = _build(tmp_path, max_rows_estimate=10)
    out = h.query("c", sql="SELECT * FROM t")
    assert out["row_count"] == 3
    rec = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["cost_check_ms"] == 0  # unsupported parser ⇒ 0ms


class _StubGuard(CostGuard):
    def __init__(self, rows: int | None, ms: int = 12) -> None:
        self._rows = rows
        self._ms = ms
        self.received_sql: str | None = None

    def check(self, sql, dialect, engine, threshold):  # noqa: ANN001
        self.received_sql = sql
        return self._rows, self._ms


def test_threshold_reject_raises_tool_error(tmp_path: Path) -> None:
    h, audit_path = _build(
        tmp_path, max_rows_estimate=10, cost_guard=_StubGuard(rows=999)
    )
    with pytest.raises(ToolError, match="cost_guard_error"):
        h.query("c", sql="SELECT * FROM t")
    rec = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["status"] == "rejected"
    assert "rows_estimate=999" in rec["reason"]
    assert rec["cost_check_ms"] == 12


def test_threshold_pass_proceeds_and_records_ms(tmp_path: Path) -> None:
    h, audit_path = _build(
        tmp_path, max_rows_estimate=1000, cost_guard=_StubGuard(rows=5, ms=7)
    )
    out = h.query("c", sql="SELECT * FROM t")
    assert out["row_count"] == 3
    rec = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["status"] == "ok"
    assert rec["cost_check_ms"] == 7


def test_parser_failure_proceeds(tmp_path: Path) -> None:
    # rows=None ⇒ parser unsupported/failed; query must still run
    h, audit_path = _build(
        tmp_path, max_rows_estimate=10, cost_guard=_StubGuard(rows=None, ms=3)
    )
    out = h.query("c", sql="SELECT * FROM t")
    assert out["row_count"] == 3
    rec = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["status"] == "ok"
    assert rec["cost_check_ms"] == 3


def test_max_rows_estimate_validator_rejects_zero() -> None:
    with pytest.raises(ValueError, match="max_rows_estimate must be > 0"):
        ConnectionConfig(url="sqlite:///x", dialect="sqlite", max_rows_estimate=0)


def test_cost_guard_receives_raw_sql_not_limit_injected(tmp_path: Path) -> None:
    # Regression: cost_guard.check() must see the un-LIMIT'd SQL so the
    # planner reports true scan size (not the auto-injected LIMIT cap).
    stub = _StubGuard(rows=5, ms=1)
    h, _ = _build(tmp_path, max_rows_estimate=1000, cost_guard=stub)
    h.query("c", sql="SELECT * FROM t")
    assert stub.received_sql is not None
    assert "LIMIT" not in stub.received_sql.upper()
    assert stub.received_sql == "SELECT * FROM t"
