"""Whole-database inspection tools against a real PostgreSQL container.

Fixtures (pg-init.sql): `products` carries comments, CHECK / UNIQUE
constraints, an unscanned index, an all-NULL and a constant column;
`shipments` has a NOT VALID foreign key with one orphan row and a duplicated
tracking number. The container preloads pg_stat_statements.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbread.tools import ToolError

from .conftest import build_handlers

psycopg2 = pytest.importorskip("psycopg2")


def _by(entries: list[dict], check: str) -> list[dict]:
    return [e for e in entries if e["check"] == check]


def test_pg_list_tables_reports_estimate_and_size(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    rows = {r["name"]: r for r in h.list_tables("t")}
    assert rows["users"]["row_estimate"] == 3  # analyzed by the init script
    assert rows["users"]["size_bytes"] > 0


def test_pg_describe_reports_comments_and_constraints(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    info = h.describe_table("t", "products", schema="public")
    assert info["comment"] == "Catalogue items"
    sku = next(c for c in info["columns"] if c["name"] == "sku")
    assert sku["comment"] == "Stock keeping unit"
    assert [c["name"] for c in info["check_constraints"]] == ["products_price_positive"]
    assert {"name": "products_sku_key", "columns": ["sku"]} in info["unique_constraints"]


def test_pg_overview_maps_tables_and_relationships(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    out = h.database_overview("t")
    assert out["schema"] == "public"
    assert out["source"] == "catalog"
    tables = {t["name"]: t for t in out["tables"]}
    assert tables["orders"]["primary_key"] == ["id"]
    assert tables["orders"]["row_estimate"] == 3
    assert {
        "from_table": "shipments", "from_columns": ["order_id"],
        "to_table": "orders", "to_columns": ["id"],
    } in out["relationships"]


def test_pg_search_schema_finds_column(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    matches = h.search_schema("t", "SKU")["matches"]
    assert {"table": "products", "column": "sku", "type": "TEXT", "kind": "column"} in matches


def test_pg_integrity_finds_orphan_and_duplicate(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    out = h.check_integrity(
        "t", "shipments", checks=["orphans", "duplicates"], unique_columns=["tracking"]
    )
    (orphans,) = _by(out["checks"], "orphans")
    assert orphans["error"] is None
    assert orphans["violations"] == 1
    assert orphans["examples"][0]["order_id"] == 999
    (dupes,) = _by(out["checks"], "duplicates")
    assert dupes["violations"] == 1
    assert dupes["examples"][0]["tracking"] == "T-1"


def test_pg_integrity_finds_null_and_constant_columns(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    out = h.check_integrity("t", "products", checks=["null_columns", "constant_columns"])
    (nulls,) = _by(out["checks"], "null_columns")
    assert {"column": "discontinued_reason"} in nulls["examples"]
    (constants,) = _by(out["checks"], "constant_columns")
    assert [e["column"] for e in constants["examples"]] == ["currency"]
    assert constants["examples"][0]["value"] == "USD"


def test_pg_constant_check_runs_on_boolean_and_json_columns(
    pg_url: str, tmp_path: Path
) -> None:
    """PostgreSQL has no MIN(boolean) and no json equality; neither may break the check."""
    h = build_handlers(pg_url, "postgres", tmp_path)
    out = h.check_integrity("t", "profiles", checks=["constant_columns"])
    # is_active holds true and false; note holds 'x' and one NULL.
    assert out["checks"] == [{
        "check": "constant_columns", "target": "profiles", "violations": 1,
        "examples": [{"column": "note", "value": "x", "null_count": 1}], "error": None,
    }]


def test_pg_integrity_rejects_references(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    with pytest.raises(ToolError, match="invalid_input: references"):
        h.check_integrity("t", "shipments", references=[{"field": "order_id", "to": "orders"}])


def test_pg_diff_schema_of_itself_is_empty(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    out = h.diff_schema("t", "t", schema_a="public", schema_b="public")
    assert out["tables_only_in_a"] == [] and out["tables_only_in_b"] == []
    assert out["changed"] == {}


def test_pg_db_health_every_section_answers(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    out = h.db_health("t")
    assert out["dialect"] == "postgres"
    sections = out["sections"]
    assert set(sections) == {
        "top_statements", "unused_indexes", "dead_tuples", "lock_waits", "long_running",
    }
    for name, section in sections.items():
        assert "rows" in section, (name, section)
    unused = {r["index_name"] for r in sections["unused_indexes"]["rows"]}
    assert "idx_products_currency" in unused


def test_pg_rejects_database_argument(pg_url: str, tmp_path: Path) -> None:
    h = build_handlers(pg_url, "postgres", tmp_path)
    with pytest.raises(ToolError, match="database is for mongodb connections only"):
        h.list_tables("t", database="other")
