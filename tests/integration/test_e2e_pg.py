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
    tables = {row["name"] for row in h.list_tables("t", schema="public")}
    assert {"users", "orders"}.issubset(tables)
    info = h.describe_table("t", "users", schema="public")
    assert any(c["pk"] for c in info["columns"])


def test_pg_describe_exposes_foreign_keys(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    fks = h.describe_table("t", "orders", schema="public")["foreign_keys"]
    assert fks[0]["references"]["table"] == "users"


def test_pg_list_schemas(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    assert "public" in h.list_schemas("t")


def test_pg_compact_json_literal(pg_url: str, tmp_path: Path) -> None:
    """':1' inside a JSON literal must not be read as a bind parameter."""
    h = build_handlers(pg_url, "postgres", tmp_path)
    assert h.query("t", sql="""SELECT '{"a":1}'::jsonb""")["rows"] == [[{"a": 1}]]


def test_pg_params_are_bound(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    out = h.query(
        "t", sql="SELECT total FROM orders WHERE user_id = :uid ORDER BY id LIMIT 1",
        params={"uid": 2},
    )
    assert out["row_count"] == 1


def test_pg_oversized_limit_is_clamped(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path, max_rows=2)
    assert h.query("t", "SELECT * FROM users LIMIT 999999")["row_count"] == 2


def test_pg_row_lock_rejected(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    with pytest.raises(ToolError, match="row_lock_not_allowed"):
        h.query("t", "SELECT * FROM users FOR UPDATE")


def test_pg_sample_and_profile(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    sample = h.sample_table("t", "orders", n=2, schema="public")
    assert sample["row_count"] == 2
    fields = {f["name"]: f for f in h.profile_table("t", "orders", schema="public")["fields"]}
    assert fields["id"]["distinct_count"] == 3
