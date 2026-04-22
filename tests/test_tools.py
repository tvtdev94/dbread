"""Integration-ish tests for ToolHandlers against real in-process SQLite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from dbread.audit import AuditLogger
from dbread.config import AuditConfig, ConnectionConfig, Settings
from dbread.connections import ConnectionManager
from dbread.rate_limiter import RateLimiter
from dbread.sql_guard import SqlGuard
from dbread.tools import ToolError, ToolHandlers


def _seed_sqlite(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"))
        conn.execute(text("CREATE INDEX idx_users_name ON users(name)"))
        conn.execute(
            text("INSERT INTO users (id, name) VALUES (1, 'alice'), (2, 'bob'), (3, 'carol')")
        )
    engine.dispose()


def _build_handlers(
    tmp_path: Path, *, rate_per_min: int = 60, max_rows: int = 100
) -> tuple[ToolHandlers, Path]:
    db_path = tmp_path / "test.db"
    _seed_sqlite(db_path)
    audit_path = tmp_path / "audit.jsonl"
    settings = Settings(
        connections={
            "test": ConnectionConfig(
                url=f"sqlite:///{db_path}",
                dialect="sqlite",
                rate_limit_per_min=rate_per_min,
                statement_timeout_s=5,
                max_rows=max_rows,
            ),
        },
        audit=AuditConfig(path=str(audit_path), rotate_mb=1),
    )
    cm = ConnectionManager(settings)
    handlers = ToolHandlers(
        settings=settings,
        conn_mgr=cm,
        guard=SqlGuard(),
        rate_limiter=RateLimiter(settings),
        audit=AuditLogger(str(audit_path), 1),
    )
    return handlers, audit_path


def test_list_connections(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    assert h.list_connections() == [{"name": "test", "dialect": "sqlite"}]


def test_list_tables(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    tables = h.list_tables("test")
    assert "users" in tables


def test_describe_table(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    info = h.describe_table("test", "users")
    names = [c["name"] for c in info["columns"]]
    assert names == ["id", "name"]
    pk_col = next(c for c in info["columns"] if c["pk"])
    assert pk_col["name"] == "id"
    assert any(i["columns"] == ["name"] for i in info["indexes"])


def test_query_happy_path(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    out = h.query("test", "SELECT id, name FROM users ORDER BY id")
    assert out["row_count"] == 3
    assert out["columns"] == ["id", "name"]
    assert out["rows"][0] == [1, "alice"]


def test_query_rejects_dml(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    with pytest.raises(ToolError, match="sql_guard"):
        h.query("test", "UPDATE users SET name='x'")


def test_query_rejects_cte_dml(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    with pytest.raises(ToolError, match="sql_guard"):
        h.query(
            "test",
            "WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d",
        )


def test_query_rate_limit(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path, rate_per_min=2)
    h.query("test", "SELECT 1")
    h.query("test", "SELECT 1")
    with pytest.raises(ToolError, match="rate_limit_exceeded"):
        h.query("test", "SELECT 1")


def test_query_limit_injected_in_audit(tmp_path: Path) -> None:
    h, audit_path = _build_handlers(tmp_path, max_rows=50)
    h.query("test", "SELECT * FROM users")
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert lines, "audit log should have at least one entry"
    rec = json.loads(lines[-1])
    assert rec["status"] == "ok"
    assert "LIMIT 50" in rec["sql"].upper()


def test_query_db_error_logged(tmp_path: Path) -> None:
    h, audit_path = _build_handlers(tmp_path)
    with pytest.raises(ToolError, match="db_error"):
        h.query("test", "SELECT * FROM nonexistent_tbl")
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    last = json.loads(lines[-1])
    assert last["status"] == "failed"


def test_explain_ok(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    out = h.explain("test", "SELECT * FROM users")
    assert "plan" in out
    assert len(out["plan"]) > 0


def test_explain_rejects_dml(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    with pytest.raises(ToolError, match="sql_guard"):
        h.explain("test", "DELETE FROM users")


def test_query_max_rows_caps_at_config(tmp_path: Path) -> None:
    h, audit_path = _build_handlers(tmp_path, max_rows=2)
    out = h.query("test", "SELECT * FROM users", max_rows=1000)
    # user asked for 1000 but config cap is 2 -> effective is 2
    assert out["row_count"] <= 2
    last = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert "LIMIT 2" in last["sql"].upper()
