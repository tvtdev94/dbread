"""Whole-database inspection tools against a real MySQL container.

Fixtures (mysql-init.sql): `products` carries comments, CHECK / UNIQUE
constraints, an all-NULL and a constant column; `shipments` holds one orphan
row (inserted with FOREIGN_KEY_CHECKS off) and a duplicated tracking number.
The read-only user may read performance_schema but not sys.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from .conftest import build_handlers

pymysql = pytest.importorskip("pymysql")


def _by(entries: list[dict], check: str) -> list[dict]:
    return [e for e in entries if e["check"] == check]


def test_mysql_list_tables_reports_estimate_and_size(mysql_url: str, tmp_path: Path) -> None:
    h = build_handlers(mysql_url, "mysql", tmp_path)
    rows = {r["name"]: r for r in h.list_tables("t")}
    assert isinstance(rows["users"]["row_estimate"], int)
    assert rows["users"]["size_bytes"] > 0


def test_mysql_describe_reports_comments_and_constraints(
    mysql_url: str, tmp_path: Path
) -> None:
    h = build_handlers(mysql_url, "mysql", tmp_path)
    info = h.describe_table("t", "products")
    assert info["comment"] == "Catalogue items"
    sku = next(c for c in info["columns"] if c["name"] == "sku")
    assert sku["comment"] == "Stock keeping unit"
    assert [c["name"] for c in info["check_constraints"]] == ["products_price_positive"]
    assert {"name": "products_sku_key", "columns": ["sku"]} in info["unique_constraints"]


def test_mysql_overview_maps_relationships(mysql_url: str, tmp_path: Path) -> None:
    h = build_handlers(mysql_url, "mysql", tmp_path)
    out = h.database_overview("t")
    assert out["schema"] == "testdb"
    assert {
        "from_table": "shipments", "from_columns": ["order_id"],
        "to_table": "orders", "to_columns": ["id"],
    } in out["relationships"]


def test_mysql_integrity_finds_orphan_and_duplicate(mysql_url: str, tmp_path: Path) -> None:
    h = build_handlers(mysql_url, "mysql", tmp_path)
    out = h.check_integrity("t", "shipments", unique_columns=["tracking"])
    (orphans,) = _by(out["checks"], "orphans")
    assert orphans["violations"] == 1
    assert orphans["examples"][0]["order_id"] == 999
    (dupes,) = _by(out["checks"], "duplicates")
    assert dupes["violations"] == 1


def test_mysql_integrity_finds_null_and_constant_columns(
    mysql_url: str, tmp_path: Path
) -> None:
    h = build_handlers(mysql_url, "mysql", tmp_path)
    out = h.check_integrity("t", "products", checks=["null_columns", "constant_columns"])
    (nulls,) = _by(out["checks"], "null_columns")
    assert {"column": "discontinued_reason"} in nulls["examples"]
    (constants,) = _by(out["checks"], "constant_columns")
    assert [e["column"] for e in constants["examples"]] == ["currency"]


def test_mysql_db_health_isolates_missing_privileges(mysql_url: str, tmp_path: Path) -> None:
    """sys.* is not granted: those sections fail with a hint, the rest answer."""
    h = build_handlers(mysql_url, "mysql", tmp_path)
    sections = h.db_health("t")["sections"]
    assert "rows" in sections["top_statements"]
    assert "rows" in sections["fragmentation"]
    for name in ("unused_indexes", "lock_waits"):
        assert sections[name]["error"].startswith("db_error"), sections[name]
        assert "GRANT SELECT ON sys.*" in sections[name]["hint"]
