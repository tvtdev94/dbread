"""E2E tests against a real PostgreSQL container.

Requires Docker. Skipped automatically if `docker` CLI is absent or
`SKIP_DOCKER=1` is set in the environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbread.tools import ToolError

from .conftest import build_handlers

psycopg2 = pytest.importorskip("psycopg2")


def test_pg_query_happy_path(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path, max_rows=50)
    out = h.query("t", "SELECT name FROM users ORDER BY id")
    assert out["row_count"] == 3
    assert out["rows"][0] == ["alice"]


def test_pg_layer_0_rejects_write(pg_url: str, tmp_path: Path) -> None:
    """Even if our guard somehow missed, the RO user must block writes."""
    h = build_handlers(pg_url, "postgres", tmp_path)
    # This is caught by Layer 1 (guard) before it reaches the DB,
    # but the DB-side default_transaction_read_only is our Layer 0.
    with pytest.raises(ToolError, match="sql_guard"):
        h.query("t", "INSERT INTO users(name) VALUES ('eve')")


def test_pg_cte_dml_blocked(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    with pytest.raises(ToolError, match="sql_guard"):
        h.query(
            "t",
            "WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d",
        )


def test_pg_list_and_describe(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    tables = h.list_tables("t", schema="public")
    assert {"users", "orders"}.issubset(set(tables))
    info = h.describe_table("t", "users", schema="public")
    assert any(c["pk"] for c in info["columns"])
