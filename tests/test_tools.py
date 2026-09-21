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
    assert {"name": "users", "type": "table"} in tables


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


# ---- introspection surfaces ------------------------------------------------


def _seed_view_and_fk(db_path: Path) -> None:
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER "
            "REFERENCES users(id), total NUMERIC, note TEXT)"
        ))
        conn.execute(text("INSERT INTO orders VALUES (1,1,10,'x'),(2,1,20,NULL),(3,2,30,NULL)"))
        conn.execute(text("CREATE VIEW active_orders AS SELECT * FROM orders"))
    engine.dispose()


def test_list_tables_includes_views(tmp_path: Path) -> None:
    """A view the agent cannot see is a view the agent decides does not exist."""
    h, _ = _build_handlers(tmp_path)
    _seed_view_and_fk(tmp_path / "test.db")
    found = h.list_tables("test")
    assert {"name": "active_orders", "type": "view"} in found
    assert {"name": "orders", "type": "table"} in found


def test_describe_table_exposes_foreign_keys(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    _seed_view_and_fk(tmp_path / "test.db")
    fks = h.describe_table("test", "orders")["foreign_keys"]
    assert len(fks) == 1
    assert fks[0]["columns"] == ["user_id"]
    assert fks[0]["references"]["table"] == "users"


def test_list_schemas(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    assert "main" in h.list_schemas("test")


# ---- row-count bounding ----------------------------------------------------


def test_caller_supplied_limit_is_clamped(tmp_path: Path) -> None:
    """An oversized LIMIT used to reach the server untouched."""
    h, audit_path = _build_handlers(tmp_path, max_rows=2)
    h.query("test", "SELECT * FROM users LIMIT 999999")
    last = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert "LIMIT 2" in last["sql"].upper()
    assert "999999" not in last["sql"]


def test_limit_below_cap_left_alone(tmp_path: Path) -> None:
    h, audit_path = _build_handlers(tmp_path, max_rows=100)
    h.query("test", "SELECT * FROM users LIMIT 2")
    last = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert "LIMIT 2" in last["sql"].upper()


def test_truncated_reported_when_rows_reach_cap(tmp_path: Path) -> None:
    """Hitting the cap always flags truncation, even if nothing was cut."""
    h, _ = _build_handlers(tmp_path, max_rows=2)
    out = h.query("test", "SELECT * FROM users")
    assert out["row_count"] == 2
    assert out["truncated"] is True


def test_not_truncated_below_cap(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path, max_rows=100)
    assert h.query("test", "SELECT * FROM users")["truncated"] is False


# ---- literals and parameters -----------------------------------------------


def test_literal_with_colon_is_not_a_bind_parameter(tmp_path: Path) -> None:
    """Compact JSON is what an agent writes; ':1' must stay a literal."""
    h, _ = _build_handlers(tmp_path)
    out = h.query("test", """SELECT '{"a":1}' AS j""")
    assert out["rows"] == [['{"a":1}']]


def test_params_are_bound(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    out = h.query("test", "SELECT name FROM users WHERE id = :uid", params={"uid": 2})
    assert out["rows"] == [["bob"]]


# ---- sample_table / profile_table ------------------------------------------


def test_sample_table_orders_by_primary_key_desc(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    out = h.sample_table("test", "users", n=2)
    assert out["ordered_by"] == "id"
    assert [r[0] for r in out["rows"]] == [3, 2]


def test_profile_table_reports_nulls_and_range(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    _seed_view_and_fk(tmp_path / "test.db")
    fields = {f["name"]: f for f in h.profile_table("test", "orders")["fields"]}
    assert fields["note"]["null_count"] == 2
    assert fields["id"]["distinct_count"] == 3
    assert fields["id"]["min"] == 1
    assert fields["id"]["max"] == 3


def test_profile_table_rejects_unknown_column(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    with pytest.raises(ToolError, match="invalid_input"):
        h.profile_table("test", "users", columns=["nope"])


def test_sample_table_unknown_table(tmp_path: Path) -> None:
    h, _ = _build_handlers(tmp_path)
    with pytest.raises(ToolError, match="unknown_table"):
        h.sample_table("test", "does_not_exist")
