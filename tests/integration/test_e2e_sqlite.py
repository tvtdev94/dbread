"""E2E pipeline test on a real SQLite file (no Docker needed).

Covers: guard -> limit inject -> rate limit -> execute -> audit, across
the full tool surface including list_tables and describe_table.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from dbread.tools import ToolError

from .conftest import build_handlers


def _seed(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE orders (id INTEGER PRIMARY KEY, "
                "user_id INT, total NUMERIC)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO users (name) VALUES ('alice'), ('bob'), ('carol')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO orders (user_id, total) VALUES (1,100),(1,200),(2,50)"
            )
        )
    engine.dispose()


@pytest.fixture
def sqlite_handlers(tmp_path: Path):
    db = tmp_path / "e2e.db"
    _seed(db)
    return build_handlers(f"sqlite:///{db}", "sqlite", tmp_path, max_rows=50)


def test_list_describe_query_chain(sqlite_handlers, tmp_path: Path) -> None:
    conns = sqlite_handlers.list_connections()
    assert conns == [{"name": "t", "dialect": "sqlite"}]

    tables = sqlite_handlers.list_tables("t")
    assert {"users", "orders"}.issubset(set(tables))

    info = sqlite_handlers.describe_table("t", "users")
    assert [c["name"] for c in info["columns"]] == ["id", "name"]

    out = sqlite_handlers.query("t", "SELECT COUNT(*) AS n FROM users")
    assert out["rows"][0] == [3]


def test_reject_paths(sqlite_handlers) -> None:
    with pytest.raises(ToolError, match="sql_guard"):
        sqlite_handlers.query("t", "UPDATE users SET name='x'")
    with pytest.raises(ToolError, match="sql_guard"):
        sqlite_handlers.query("t", "SELECT 1; DROP TABLE users")


def test_audit_records_ok_and_rejected(
    sqlite_handlers, tmp_path: Path
) -> None:
    sqlite_handlers.query("t", "SELECT * FROM users")
    with pytest.raises(ToolError):
        sqlite_handlers.query("t", "DELETE FROM users")

    audit_path = tmp_path / "audit.jsonl"
    records = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    statuses = [r["status"] for r in records]
    assert "ok" in statuses
    assert "rejected" in statuses


def test_rate_limit_triggers_at_threshold(tmp_path: Path) -> None:
    db = tmp_path / "rate.db"
    _seed(db)
    handlers = build_handlers(
        f"sqlite:///{db}", "sqlite", tmp_path, rate_per_min=3, max_rows=10
    )
    handlers.query("t", "SELECT 1")
    handlers.query("t", "SELECT 1")
    handlers.query("t", "SELECT 1")
    with pytest.raises(ToolError, match="rate_limit_exceeded"):
        handlers.query("t", "SELECT 1")
