"""Unit tests for phase-02 cost guard dialects (MSSQL/Oracle/DuckDB/Mongo).

Uses fake SQLAlchemy-shaped engines and a fake Mongo db — no Docker,
no pymongo import.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any

from dbread.cost_guard import CostGuard
from dbread.mongo.cost_guard import MongoCostGuard

# ---------------------------------------------------------------------------
# Fake SQLAlchemy engine — supports multi-stage execute() for SHOWPLAN_XML
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row: tuple | None = None, rows: list[tuple] | None = None) -> None:
        self._row = row
        self._rows = rows or []

    def fetchone(self) -> tuple | None:
        return self._row

    def fetchall(self) -> list[tuple]:
        return self._rows


class _FakeConn:
    def __init__(self, responder: Any) -> None:
        self._responder = responder
        self.executed: list[str] = []

    def execute(self, stmt: Any, bind: dict | None = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append(sql)
        if isinstance(self._responder, Exception):
            raise self._responder
        out = self._responder(sql) if callable(self._responder) else self._responder
        if isinstance(out, list):
            return _FakeResult(rows=out)
        if isinstance(out, tuple):
            return _FakeResult(row=out)
        if out is None:
            return _FakeResult(row=None)
        return _FakeResult(row=(out,))


class _FakeEngine:
    def __init__(self, responder: Any) -> None:
        self.responder = responder
        self.last_conn: _FakeConn | None = None

    @contextmanager
    def connect(self):  # noqa: ANN201
        self.last_conn = _FakeConn(self.responder)
        try:
            yield self.last_conn
        finally:
            pass


# ---------------------------------------------------------------------------
# MSSQL — SHOWPLAN_XML
# ---------------------------------------------------------------------------


_MSSQL_PLAN_XML = """<?xml version="1.0"?>
<ShowPlanXML xmlns="http://schemas.microsoft.com/sqlserver/2004/07/showplan">
  <BatchSequence>
    <Batch>
      <Statements>
        <StmtSimple StatementText="SELECT *" StatementEstRows="12345.5" />
      </Statements>
    </Batch>
  </BatchSequence>
</ShowPlanXML>"""


def _mssql_responder(sql: str) -> Any:
    if "SHOWPLAN_XML ON" in sql or "SHOWPLAN_XML OFF" in sql:
        return None
    return _MSSQL_PLAN_XML


def test_mssql_parser_extracts_estimated_rows() -> None:
    rows, ms = CostGuard().check(
        "SELECT * FROM t", "mssql", _FakeEngine(_mssql_responder), threshold=1000
    )
    assert rows == 12345
    assert ms >= 0


def test_mssql_parser_handles_no_namespace_xml() -> None:
    plain = '<root><StmtSimple StatementEstRows="42" /></root>'

    def resp(sql: str) -> Any:
        if "SHOWPLAN_XML" in sql:
            return None
        return plain

    rows, _ = CostGuard().check("SELECT 1", "mssql", _FakeEngine(resp), threshold=1)
    assert rows == 42


def test_mssql_parser_malformed_xml_fails_open() -> None:
    def resp(sql: str) -> Any:
        if "SHOWPLAN_XML" in sql:
            return None
        return "<<<not xml>>>"

    rows, _ = CostGuard().check("SELECT 1", "mssql", _FakeEngine(resp), threshold=1)
    assert rows is None


def test_mssql_parser_no_stmtsimple_fails_open() -> None:
    def resp(sql: str) -> Any:
        if "SHOWPLAN_XML" in sql:
            return None
        return "<root><other /></root>"

    rows, _ = CostGuard().check("SELECT 1", "mssql", _FakeEngine(resp), threshold=1)
    assert rows is None


class _FakeConnTrackInvalidate(_FakeConn):
    def __init__(self, responder: Any, on_off_fail: bool = False) -> None:
        super().__init__(responder)
        self.invalidated = False
        self._on_off_fail = on_off_fail

    def execute(self, stmt: Any) -> _FakeResult:
        sql = str(stmt)
        if self._on_off_fail and "SHOWPLAN_XML OFF" in sql:
            raise RuntimeError("session crashed during OFF")
        return super().execute(stmt)

    def invalidate(self) -> None:
        self.invalidated = True


class _FakeEngineTrackInvalidate(_FakeEngine):
    def __init__(self, responder: Any, on_off_fail: bool = False) -> None:
        super().__init__(responder)
        self._on_off_fail = on_off_fail

    @contextmanager
    def connect(self):  # noqa: ANN201
        self.last_conn = _FakeConnTrackInvalidate(self.responder, self._on_off_fail)
        try:
            yield self.last_conn
        finally:
            pass


def test_mssql_parser_invalidates_conn_on_showplan_off_failure() -> None:
    # If `SET SHOWPLAN_XML OFF` raises, the session is still in plan-emit
    # mode → must invalidate so pool drops the connection.
    engine = _FakeEngineTrackInvalidate(_mssql_responder, on_off_fail=True)
    rows, _ = CostGuard().check("SELECT 1", "mssql", engine, threshold=1)
    # Parser still returns the parsed value even though OFF failed
    assert rows == 12345
    # And the conn was marked invalid for the pool
    assert engine.last_conn.invalidated is True


# ---------------------------------------------------------------------------
# Oracle — EXPLAIN PLAN FOR
# ---------------------------------------------------------------------------


def _oracle_responder(sql: str) -> Any:
    if "EXPLAIN PLAN" in sql and "FOR" in sql:
        return None
    if "DELETE FROM PLAN_TABLE" in sql:
        return None
    if "PLAN_TABLE" in sql:
        return (5000,)
    return None


def test_oracle_parser_extracts_cardinality() -> None:
    rows, ms = CostGuard().check(
        "SELECT * FROM t", "oracle", _FakeEngine(_oracle_responder), threshold=100
    )
    assert rows == 5000
    assert ms >= 0


def test_oracle_parser_no_row_fails_open() -> None:
    def resp(sql: str) -> Any:
        return None  # neither EXPLAIN nor PLAN_TABLE return data

    rows, _ = CostGuard().check("SELECT 1", "oracle", _FakeEngine(resp), threshold=1)
    assert rows is None


def test_oracle_parser_grant_denied_fails_open() -> None:
    # Simulate INSERT-on-PLAN_TABLE perm denied
    rows, _ = CostGuard().check(
        "SELECT 1", "oracle",
        _FakeEngine(RuntimeError("ORA-01031: insufficient privileges")),
        threshold=1,
    )
    assert rows is None


class _OracleStmtIdRecorder:
    """Capture the per-call STATEMENT_ID + ensure DELETE issued."""

    def __init__(self) -> None:
        self.explain_stmt_ids: list[str] = []
        self.delete_stmt_ids: list[str] = []
        self.select_bind: dict | None = None

    def __call__(self, sql: str, bind: dict | None = None) -> Any:
        if "EXPLAIN PLAN SET STATEMENT_ID" in sql:
            # Extract id between quotes
            m = re.search(r"STATEMENT_ID\s*=\s*'([^']+)'", sql)
            if m:
                self.explain_stmt_ids.append(m.group(1))
            return None
        if "SELECT cardinality FROM PLAN_TABLE" in sql:
            self.select_bind = bind
            return (4242,)
        if "DELETE FROM PLAN_TABLE" in sql:
            if bind:
                self.delete_stmt_ids.append(bind.get("sid"))
            return None
        return None


class _FakeConnWithBind(_FakeConn):
    def __init__(self, recorder: _OracleStmtIdRecorder) -> None:
        super().__init__(recorder)
        self._recorder = recorder

    def execute(self, stmt: Any, bind: dict | None = None) -> _FakeResult:
        sql = str(stmt)
        out = self._recorder(sql, bind)
        if isinstance(out, tuple):
            return _FakeResult(row=out)
        return _FakeResult(row=None)


class _FakeEngineWithBind(_FakeEngine):
    def __init__(self, recorder: _OracleStmtIdRecorder) -> None:
        super().__init__(recorder)
        self.recorder = recorder

    @contextmanager
    def connect(self):  # noqa: ANN201
        self.last_conn = _FakeConnWithBind(self.recorder)
        try:
            yield self.last_conn
        finally:
            pass


def test_oracle_parser_uses_unique_statement_id_and_cleans_up() -> None:
    rec = _OracleStmtIdRecorder()
    engine = _FakeEngineWithBind(rec)
    rows, _ = CostGuard().check("SELECT * FROM t", "oracle", engine, threshold=10)
    assert rows == 4242
    # 1 explain → 1 statement_id captured
    assert len(rec.explain_stmt_ids) == 1
    sid = rec.explain_stmt_ids[0]
    # statement_id is dbread_<16 hex>
    assert sid.startswith("dbread_") and len(sid) == 7 + 16
    # SELECT used the same sid as bind param
    assert rec.select_bind == {"sid": sid}
    # DELETE issued in finally block with the same sid
    assert rec.delete_stmt_ids == [sid]


def test_oracle_parser_unique_id_per_call() -> None:
    # Two consecutive calls must produce DIFFERENT statement_ids.
    rec = _OracleStmtIdRecorder()
    engine = _FakeEngineWithBind(rec)
    cg = CostGuard()
    cg.check("SELECT 1 FROM dual", "oracle", engine, threshold=1)
    cg.check("SELECT 2 FROM dual", "oracle", engine, threshold=1)
    assert len(rec.explain_stmt_ids) == 2
    assert rec.explain_stmt_ids[0] != rec.explain_stmt_ids[1]


# ---------------------------------------------------------------------------
# DuckDB — EXPLAIN text
# ---------------------------------------------------------------------------


def test_duckdb_parser_extracts_ec() -> None:
    plan_lines = [
        ("│  ┌─────────────┐    │",),
        ("│  │  SEQ_SCAN   │    │",),
        ("│  │   EC: 7777  │    │",),
        ("│  └─────────────┘    │",),
    ]
    rows, _ = CostGuard().check(
        "SELECT * FROM t", "duckdb", _FakeEngine(plan_lines), threshold=10
    )
    assert rows == 7777


def test_duckdb_parser_extracts_estimated_cardinality_long_form() -> None:
    plan = [("Estimated Cardinality: 4242",)]
    rows, _ = CostGuard().check("SELECT 1", "duckdb", _FakeEngine(plan), threshold=10)
    assert rows == 4242


def test_duckdb_parser_no_estimate_fails_open() -> None:
    plan = [("just some plan text",), ("no number here",)]
    rows, _ = CostGuard().check("SELECT 1", "duckdb", _FakeEngine(plan), threshold=10)
    assert rows is None


def test_duckdb_parser_takes_max_to_defeat_literal_injection() -> None:
    # User-injected `SELECT 'EC: 0' …` would echo into plan text. max() of all
    # matches ensures the real plan EC dominates the injected zero.
    plan = [
        ("│  filter: 'EC: 0'  │",),
        ("│  EC: 50000        │",),
    ]
    rows, _ = CostGuard().check("SELECT 1", "duckdb", _FakeEngine(plan), threshold=10)
    assert rows == 50000


def test_duckdb_parser_word_boundary_skips_block_ec_prefix() -> None:
    # `\bEC\b` must not match `BLOCK_EC: 999` (different identifier prefix).
    plan = [("│  BLOCK_EC: 999 │",), ("│  EC: 7 │",)]
    rows, _ = CostGuard().check("SELECT 1", "duckdb", _FakeEngine(plan), threshold=10)
    assert rows == 7


# ---------------------------------------------------------------------------
# MongoCostGuard — fake db object
# ---------------------------------------------------------------------------


class _FakeDb:
    def __init__(self, explain: dict | Exception, coll_count: int = 1_000_000) -> None:
        self._explain = explain
        self._coll_count = coll_count
        self.calls: list[tuple[str, ...]] = []

    def command(self, *args, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append((args[0], *args[1:]))
        if args[0] == "explain":
            if isinstance(self._explain, Exception):
                raise self._explain
            return self._explain
        if args[0] == "collStats":
            return {"count": self._coll_count}
        raise RuntimeError(f"unexpected command: {args[0]}")


_PLAN_COLLSCAN = {
    "queryPlanner": {
        "namespace": "appdb.users",
        "winningPlan": {"stage": "COLLSCAN", "filter": {}},
    }
}

_PLAN_IXSCAN = {
    "queryPlanner": {
        "namespace": "appdb.users",
        "winningPlan": {
            "stage": "FETCH",
            "inputStage": {"stage": "IXSCAN", "indexName": "_id_"},
        },
    }
}

_PLAN_AGG_NESTED_COLLSCAN = {
    "stages": [
        {"$cursor": {"queryPlanner": {
            "namespace": "appdb.events",
            "winningPlan": {"stage": "COLLSCAN"},
        }}},
        {"$group": {}},
    ]
}


def test_mongo_cost_guard_collscan_returns_collection_size() -> None:
    db = _FakeDb(_PLAN_COLLSCAN, coll_count=10_000_000)
    docs, ms = MongoCostGuard().estimate_docs(
        db, {"find": "users"}, threshold=1_000_000
    )
    assert docs == 10_000_000
    assert ms >= 0
    assert ("explain", {"find": "users"}) in [(c[0], c[1]) for c in db.calls]


def test_mongo_cost_guard_ixscan_only_fails_open() -> None:
    # Index plan → no COLLSCAN → return None (fail-open)
    db = _FakeDb(_PLAN_IXSCAN)
    docs, _ = MongoCostGuard().estimate_docs(
        db, {"find": "users"}, threshold=100
    )
    assert docs is None


def test_mongo_cost_guard_aggregation_with_nested_collscan() -> None:
    db = _FakeDb(_PLAN_AGG_NESTED_COLLSCAN, coll_count=500_000)
    docs, _ = MongoCostGuard().estimate_docs(
        db, {"aggregate": "events", "pipeline": []}, threshold=10_000
    )
    assert docs == 500_000


def test_mongo_cost_guard_threshold_none_short_circuits() -> None:
    db = _FakeDb(_PLAN_COLLSCAN)
    docs, ms = MongoCostGuard().estimate_docs(db, {"find": "x"}, threshold=None)
    assert (docs, ms) == (None, 0)
    assert db.calls == []  # explain MUST NOT be invoked


def test_mongo_cost_guard_explain_failure_fails_open() -> None:
    db = _FakeDb(RuntimeError("not authorized"))
    docs, _ = MongoCostGuard().estimate_docs(
        db, {"find": "x"}, threshold=10
    )
    assert docs is None


def test_mongo_cost_guard_missing_namespace_uses_command_fallback() -> None:
    plan_no_ns = {"queryPlanner": {"winningPlan": {"stage": "COLLSCAN"}}}
    db = _FakeDb(plan_no_ns, coll_count=42)
    docs, _ = MongoCostGuard().estimate_docs(
        db, {"find": "events"}, threshold=10
    )
    assert docs == 42
    # collStats should be called with collection name from command fallback
    coll_stats_calls = [c for c in db.calls if c[0] == "collStats"]
    assert coll_stats_calls and coll_stats_calls[0][1] == "events"


def test_mongo_cost_guard_collstats_missing_count_fails_open() -> None:
    class _DbNoCount:
        def command(self, *args, **kwargs):  # noqa: ANN001, ANN201
            if args[0] == "explain":
                return _PLAN_COLLSCAN
            return {}  # collStats without 'count' key

    docs, _ = MongoCostGuard().estimate_docs(_DbNoCount(), {"find": "x"}, threshold=1)
    assert docs is None
