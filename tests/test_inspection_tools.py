"""Whole-database inspection tools against real in-process SQLite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from dbread.audit import AuditLogger
from dbread.config import AuditConfig, ConnectionConfig, Settings
from dbread.connections import ConnectionManager
from dbread.inspection.health import SECTIONS, Section
from dbread.rate_limiter import RateLimiter
from dbread.sql_guard import SqlGuard
from dbread.tools import ToolError, ToolHandlers

SHOP = (
    "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT, country TEXT, legacy TEXT, "
    "CONSTRAINT users_email_key UNIQUE (email), CONSTRAINT users_id_positive CHECK (id > 0))",
    "INSERT INTO users VALUES (1, 'a@x', 'VN', NULL), (2, 'b@x', 'VN', NULL), "
    "(3, 'c@x', 'VN', NULL)",
    "CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), "
    "parent_id INTEGER REFERENCES orders(id), ref TEXT)",
    # SQLite does not enforce foreign keys by default, so orphans can exist.
    "INSERT INTO orders VALUES (1, 1, NULL, 'R1'), (2, 99, 1, 'R1'), (3, NULL, 77, 'R2'), "
    "(4, 2, 1, 'R1')",
    "CREATE INDEX idx_orders_ref ON orders(ref)",
    "CREATE VIEW big_orders AS SELECT * FROM orders",
)


def _seed(path: Path, statements: tuple[str, ...]) -> str:
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
    engine.dispose()
    return url


def _handlers(
    tmp_path: Path, dbs: dict[str, tuple[str, ...]], *, rate_per_min: int = 600
) -> tuple[ToolHandlers, Path]:
    audit_path = tmp_path / "audit.jsonl"
    settings = Settings(
        connections={
            name: ConnectionConfig(
                url=_seed(tmp_path / f"{name}.db", stmts),
                dialect="sqlite",
                rate_limit_per_min=rate_per_min,
            )
            for name, stmts in dbs.items()
        },
        audit=AuditConfig(path=str(audit_path), rotate_mb=1),
    )
    handlers = ToolHandlers(
        settings=settings,
        conn_mgr=ConnectionManager(settings),
        guard=SqlGuard(),
        rate_limiter=RateLimiter(settings),
        audit=AuditLogger(str(audit_path), 1),
    )
    return handlers, audit_path


def _checks(out: dict, check: str) -> list[dict]:
    return [c for c in out["checks"] if c["check"] == check]


# ---- list_tables / describe_table extras ----------------------------------


def test_list_tables_has_stat_keys_even_without_catalog_stats(tmp_path: Path) -> None:
    h, _ = _handlers(tmp_path, {"s": SHOP})
    rows = {r["name"]: r for r in h.list_tables("s")}
    assert rows["users"] == {
        "name": "users", "type": "table", "row_estimate": None, "size_bytes": None,
    }
    assert rows["big_orders"]["type"] == "view"


def test_describe_table_reports_constraints(tmp_path: Path) -> None:
    h, _ = _handlers(tmp_path, {"s": SHOP})
    info = h.describe_table("s", "users")
    assert info["comment"] is None  # SQLite has no table comments
    assert {"name": "users_email_key", "columns": ["email"]} in info["unique_constraints"]
    assert info["check_constraints"] == [{"name": "users_id_positive", "sqltext": "id > 0"}]
    assert all(c["comment"] is None for c in info["columns"])


# ---- database_overview ----------------------------------------------------


def test_overview_maps_tables_and_foreign_keys(tmp_path: Path) -> None:
    h, _ = _handlers(tmp_path, {"s": SHOP})
    out = h.database_overview("s")
    assert out["connection"] == "s"
    assert out["schema"] == "main"
    assert out["source"] == "catalog"
    tables = {t["name"]: t for t in out["tables"]}
    assert tables["orders"]["columns"] == 4
    assert tables["orders"]["primary_key"] == ["id"]
    assert tables["big_orders"]["type"] == "view"
    assert tables["big_orders"]["primary_key"] == []
    assert sorted(
        (r["from_table"], r["from_columns"][0], r["to_table"]) for r in out["relationships"]
    ) == [("orders", "parent_id", "orders"), ("orders", "user_id", "users")]


# ---- search_schema ---------------------------------------------------------


def test_search_is_case_insensitive_over_tables_and_columns(tmp_path: Path) -> None:
    h, _ = _handlers(tmp_path, {"s": SHOP})
    out = h.search_schema("s", "USER")
    assert {"table": "users", "column": None, "type": "table", "kind": "table"} in out["matches"]
    assert {"table": "orders", "column": "user_id", "type": "INTEGER",
            "kind": "column"} in out["matches"]
    assert out["truncated"] is False


def test_search_caps_matches(tmp_path: Path) -> None:
    columns = ", ".join(f"field_{i} TEXT" for i in range(250))
    h, _ = _handlers(tmp_path, {"s": (f"CREATE TABLE wide ({columns})",)})
    out = h.search_schema("s", "field_")
    assert len(out["matches"]) == 200
    assert out["truncated"] is True


def test_search_rejects_empty_pattern(tmp_path: Path) -> None:
    h, _ = _handlers(tmp_path, {"s": SHOP})
    with pytest.raises(ToolError, match="invalid_input: pattern"):
        h.search_schema("s", "  ")


def test_catalog_read_costs_one_token_and_one_audit_row(tmp_path: Path) -> None:
    h, audit_path = _handlers(tmp_path, {"s": SHOP}, rate_per_min=1)
    h.search_schema("s", "user")
    with pytest.raises(ToolError, match="rate_limit_exceeded"):
        h.search_schema("s", "user")
    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert [(r["sql"], r["status"]) for r in records] == [
        ("-- search_schema", "ok"), ("-- search_schema", "rejected"),
    ]


# ---- check_integrity -------------------------------------------------------


def test_integrity_finds_orphans_per_foreign_key(tmp_path: Path) -> None:
    h, _ = _handlers(tmp_path, {"s": SHOP})
    out = h.check_integrity("s", "orders", checks=["orphans"])
    by_target = {c["target"]: c for c in _checks(out, "orphans")}
    assert by_target["user_id -> users(id)"]["violations"] == 1
    assert by_target["user_id -> users(id)"]["examples"] == [{"id": 2, "user_id": 99}]
    # Self-reference: the parent side is aliased, so the join is not ambiguous.
    assert by_target["parent_id -> orders(id)"]["examples"] == [{"id": 3, "parent_id": 77}]


def test_integrity_counts_duplicate_groups(tmp_path: Path) -> None:
    h, _ = _handlers(tmp_path, {"s": SHOP})
    out = h.check_integrity("s", "orders", checks=["duplicates"], unique_columns=["ref"])
    (dupes,) = _checks(out, "duplicates")
    assert dupes["target"] == "ref"
    assert dupes["violations"] == 1
    assert dupes["examples"] == [{"ref": "R1", "duplicate_count": 3}]


def test_integrity_skips_duplicates_without_unique_columns(tmp_path: Path) -> None:
    h, _ = _handlers(tmp_path, {"s": SHOP})
    out = h.check_integrity("s", "users")
    assert _checks(out, "duplicates") == []
    assert _checks(out, "orphans") == []  # users declares no foreign keys


def test_integrity_finds_null_and_constant_columns(tmp_path: Path) -> None:
    h, _ = _handlers(tmp_path, {"s": SHOP})
    out = h.check_integrity("s", "users", checks=["null_columns", "constant_columns"])
    (nulls,) = _checks(out, "null_columns")
    assert nulls["violations"] == 1
    assert nulls["examples"] == [{"column": "legacy"}]
    (constants,) = _checks(out, "constant_columns")
    assert constants["examples"] == [{"column": "country", "value": "VN", "null_count": 0}]


def test_integrity_one_failing_check_does_not_stop_the_rest(tmp_path: Path) -> None:
    """Two tokens cover the orphan count + examples; later checks hit the limit."""
    h, _ = _handlers(tmp_path, {"s": SHOP}, rate_per_min=2)
    out = h.check_integrity(
        "s", "orders", checks=["orphans", "null_columns"], unique_columns=None
    )
    first, *rest = out["checks"]
    assert first["error"] is None and first["violations"] == 1
    assert rest, "the remaining checks must still be reported"
    for entry in rest:
        assert entry["violations"] is None
        assert entry["error"].startswith("rate_limit_exceeded")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"checks": ["nope"]}, "unknown check"),
        ({"checks": "orphans"}, "checks must be a list of strings"),
        ({"unique_columns": "ref"}, "unique_columns must be a list of strings"),
        ({"unique_columns": ["missing"]}, "unknown column: missing"),
        ({"references": [{"field": "user_id", "to": "users"}]}, "references is for mongodb"),
    ],
)
def test_integrity_rejects_bad_input(tmp_path: Path, kwargs: dict, message: str) -> None:
    h, _ = _handlers(tmp_path, {"s": SHOP})
    with pytest.raises(ToolError, match=f"invalid_input: {message}"):
        h.check_integrity("s", "orders", **kwargs)


def test_integrity_runs_through_the_audited_query_path(tmp_path: Path) -> None:
    h, audit_path = _handlers(tmp_path, {"s": SHOP})
    h.check_integrity("s", "orders", checks=["orphans"])
    sqls = [json.loads(line)["sql"] for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert any("LEFT OUTER JOIN users AS parent" in s for s in sqls)


# ---- diff_schema -----------------------------------------------------------


def test_diff_schema_reports_table_column_and_index_changes(tmp_path: Path) -> None:
    a = (
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
        "CREATE INDEX idx_users_name ON users(name)",
        "CREATE TABLE legacy (id INTEGER)",
    )
    b = (
        "CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(10), email TEXT)",
        "CREATE TABLE audit (id INTEGER)",
    )
    h, _ = _handlers(tmp_path, {"a": a, "b": b})
    out = h.diff_schema("a", "b")
    assert out == {
        "a": "a",
        "b": "b",
        "tables_only_in_a": ["legacy"],
        "tables_only_in_b": ["audit"],
        "changed": {
            "users": {
                "columns_only_in_a": [],
                "columns_only_in_b": ["email"],
                "columns_changed": {
                    "name": {
                        "a": {"type": "TEXT", "nullable": False},
                        "b": {"type": "VARCHAR(10)", "nullable": True},
                    },
                },
                "indexes_only_in_a": ["idx_users_name"],
                "indexes_only_in_b": [],
                "indexes_changed": {},
            },
        },
    }


def test_diff_schema_of_identical_databases_is_empty(tmp_path: Path) -> None:
    h, _ = _handlers(tmp_path, {"a": SHOP, "b": SHOP})
    out = h.diff_schema("a", "b")
    assert out["tables_only_in_a"] == out["tables_only_in_b"] == []
    assert out["changed"] == {}


# ---- db_health -------------------------------------------------------------


def test_db_health_rejects_unsupported_dialect(tmp_path: Path) -> None:
    h, _ = _handlers(tmp_path, {"s": SHOP})
    with pytest.raises(ToolError, match="unsupported_dialect: sqlite"):
        h.db_health("s")


def test_db_health_isolates_a_failing_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real statements on SQLite: one section reads, one hits a missing relation."""
    monkeypatch.setitem(SECTIONS, "sqlite", {
        "tables": Section("SELECT name FROM sqlite_master WHERE type = 'table'", "unused"),
        "stats": Section("SELECT * FROM missing_stats_view", "GRANT the stats role"),
    })
    h, _ = _handlers(tmp_path, {"s": SHOP})
    out = h.db_health("s")
    assert out["dialect"] == "sqlite"
    assert {"name": "users"} in out["sections"]["tables"]["rows"]
    stats = out["sections"]["stats"]
    assert stats["error"].startswith("db_error")
    assert stats["hint"] == "GRANT the stats role"


def test_db_health_gives_no_grant_hint_for_a_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(SECTIONS, "sqlite", {
        "first": Section("SELECT 1 AS one", "hint one"),
        "second": Section("SELECT 2 AS two", "hint two"),
    })
    h, _ = _handlers(tmp_path, {"s": SHOP}, rate_per_min=1)
    sections = h.db_health("s")["sections"]
    assert sections["first"] == {"rows": [{"one": 1}]}
    assert sections["second"]["error"].startswith("rate_limit_exceeded")
    assert sections["second"]["hint"] is None
