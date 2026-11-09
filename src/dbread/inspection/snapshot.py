"""Schema snapshots and the pure diff between two of them.

A snapshot is backend-neutral: the SQL side is built here from the
inspector, the MongoDB side comes from `MongoToolHandlers.schema_snapshot`,
and `diff` never needs to know which produced which.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine.reflection import Inspector

from .catalog import columns_by_table


def sql_snapshot(
    insp: Inspector, schema: str | None, relations: list[dict[str, Any]]
) -> dict[str, Any]:
    columns = columns_by_table(insp, schema)
    indexes = {key[1]: idx for key, idx in insp.get_multi_indexes(schema=schema).items()}
    tables: dict[str, Any] = {}
    for rel in relations:
        name = rel["name"]
        tables[name] = {
            "type": rel["type"],
            "columns": {
                c["name"]: {"type": str(c["type"]), "nullable": bool(c.get("nullable", True))}
                for c in columns.get(name, [])
            },
            "indexes": {
                _index_key(i): {
                    "columns": [c for c in i.get("column_names", []) if c is not None],
                    "unique": bool(i.get("unique", False)),
                }
                for i in indexes.get(name, [])
            },
        }
    return {"tables": tables}


def diff(a: str, b: str, snap_a: dict[str, Any], snap_b: dict[str, Any]) -> dict[str, Any]:
    """What differs between two snapshots; tables identical on both sides are omitted."""
    tables_a, tables_b = snap_a["tables"], snap_b["tables"]
    changed: dict[str, Any] = {}
    for name in sorted(tables_a.keys() & tables_b.keys()):
        entry = _diff_table(tables_a[name], tables_b[name])
        if entry is not None:
            changed[name] = entry
    return {
        "a": a,
        "b": b,
        "tables_only_in_a": sorted(tables_a.keys() - tables_b.keys()),
        "tables_only_in_b": sorted(tables_b.keys() - tables_a.keys()),
        "changed": changed,
    }


def _diff_table(ta: dict[str, Any], tb: dict[str, Any]) -> dict[str, Any] | None:
    cols = _diff_members(ta.get("columns", {}), tb.get("columns", {}))
    idx = _diff_members(ta.get("indexes", {}), tb.get("indexes", {}))
    entry = {
        "columns_only_in_a": cols[0],
        "columns_only_in_b": cols[1],
        "columns_changed": cols[2],
        "indexes_only_in_a": idx[0],
        "indexes_only_in_b": idx[1],
        "indexes_changed": idx[2],
    }
    return entry if any(entry.values()) else None


def _diff_members(
    a: dict[str, Any], b: dict[str, Any]
) -> tuple[list[str], list[str], dict[str, Any]]:
    only_a = sorted(a.keys() - b.keys())
    only_b = sorted(b.keys() - a.keys())
    changed = {
        name: {"a": a[name], "b": b[name]}
        for name in sorted(a.keys() & b.keys())
        if a[name] != b[name]
    }
    return only_a, only_b, changed


def _index_key(index: dict[str, Any]) -> str:
    """Unnamed indexes (some dialects) are keyed by their column list."""
    name = index.get("name")
    if name:
        return str(name)
    return "(" + ",".join(str(c) for c in index.get("column_names", [])) + ")"
