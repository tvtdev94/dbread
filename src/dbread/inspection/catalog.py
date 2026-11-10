"""Inspector-based catalog reads: relations, describe, overview, search.

Everything here talks to the SQLAlchemy inspector or reshapes rows the
caller already fetched. Statements that read catalog statistics are only
*built* here; the caller runs them through the guarded query path.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

from sqlalchemy.engine.reflection import Inspector, ObjectKind

# Search results stop here so a short pattern on a wide schema cannot return
# tens of thousands of matches.
MAX_SEARCH_MATCHES = 200
# Relations `database_overview` and `diff_schema` describe per call, first by
# name; past it the result says `truncated`. Keeps one call's payload bounded
# on schemas with thousands of tables.
MAX_RELATIONS = 500

# Row estimate + on-disk size per relation. `:schema` is bound, the LIMIT is
# an int formatted in, so the text needs no further escaping. Views are left
# out on PostgreSQL: their reltuples and size are meaningless.
_STATS_SQL = {
    "postgres": (
        "SELECT c.relname AS name, "
        "CASE WHEN c.reltuples < 0 THEN NULL ELSE CAST(c.reltuples AS BIGINT) END "
        "AS row_estimate, pg_total_relation_size(c.oid) AS size_bytes "
        "FROM pg_catalog.pg_class AS c "
        "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = :schema AND c.relkind IN ('r', 'p', 'm', 'f') "
        "LIMIT {limit}"
    ),
    "mysql": (
        "SELECT TABLE_NAME AS name, TABLE_ROWS AS row_estimate, "
        "DATA_LENGTH + INDEX_LENGTH AS size_bytes "
        "FROM information_schema.TABLES WHERE TABLE_SCHEMA = :schema "
        "LIMIT {limit}"
    ),
}


def list_relations(insp: Inspector, schema: str | None) -> list[dict[str, Any]]:
    """Every queryable relation, tagged table / view / materialized_view."""
    found = [{"name": n, "type": "table"} for n in insp.get_table_names(schema=schema)]
    found += [{"name": n, "type": "view"} for n in insp.get_view_names(schema=schema)]
    # Only a few dialects implement materialized views; the rest raise.
    with contextlib.suppress(NotImplementedError, AttributeError):
        found += [
            {"name": n, "type": "materialized_view"}
            for n in insp.get_materialized_view_names(schema=schema)
        ]
    return sorted(found, key=lambda r: r["name"])


def capped(relations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """The first MAX_RELATIONS relations (already sorted) and whether any were cut."""
    return relations[:MAX_RELATIONS], len(relations) > MAX_RELATIONS


def stats_statement(dialect: str, limit: int) -> str | None:
    """The catalog query for row estimates and sizes, or None if unsupported."""
    template = _STATS_SQL.get(dialect)
    return None if template is None else template.format(limit=int(limit))


def attach_stats(relations: list[dict[str, Any]], rows: list[list[Any]]) -> None:
    """Add `row_estimate` / `size_bytes` to every relation, None when unknown."""
    stats = {row[0]: (row[1], row[2]) for row in rows}
    for rel in relations:
        estimate, size = stats.get(rel["name"], (None, None))
        rel["row_estimate"] = _as_int(estimate)
        rel["size_bytes"] = _as_int(size)


def describe(insp: Inspector, table: str, schema: str | None) -> dict[str, Any]:
    columns = insp.get_columns(table, schema=schema)
    pks = insp.get_pk_constraint(table, schema=schema).get("constrained_columns", [])
    comment = _optional(insp.get_table_comment, table, schema, default={})
    return {
        "comment": comment.get("text"),
        "columns": [
            {
                "name": c["name"],
                "type": str(c["type"]),
                "nullable": c.get("nullable", True),
                "pk": c["name"] in pks,
                "default": None if c.get("default") is None else str(c["default"]),
                "comment": c.get("comment"),
            }
            for c in columns
        ],
        "indexes": [
            {
                "name": i.get("name"),
                "columns": i.get("column_names", []),
                "unique": i.get("unique", False),
            }
            for i in insp.get_indexes(table, schema=schema)
        ],
        # The join map. Without it an agent guesses at relationships or has
        # to go read pg_constraint by hand.
        "foreign_keys": [
            {
                "name": fk.get("name"),
                "columns": fk.get("constrained_columns", []),
                "references": {
                    "schema": fk.get("referred_schema"),
                    "table": fk.get("referred_table"),
                    "columns": fk.get("referred_columns", []),
                },
            }
            for fk in insp.get_foreign_keys(table, schema=schema)
        ],
        "check_constraints": [
            {"name": c.get("name"), "sqltext": c.get("sqltext")}
            for c in _optional(insp.get_check_constraints, table, schema, default=[])
        ],
        "unique_constraints": [
            {"name": u.get("name"), "columns": u.get("column_names", [])}
            for u in _optional(insp.get_unique_constraints, table, schema, default=[])
        ],
    }


def columns_by_table(
    insp: Inspector, schema: str | None, names: list[str] | None = None
) -> dict[str, list[dict]]:
    """Columns of every relation (or of `names`) in one inspector pass."""
    multi = insp.get_multi_columns(schema=schema, kind=ObjectKind.ANY, filter_names=names)
    return {key[1]: cols for key, cols in multi.items()}


def overview(
    insp: Inspector, schema: str | None, relations: list[dict[str, Any]]
) -> dict[str, Any]:
    names = [r["name"] for r in relations]
    columns = columns_by_table(insp, schema, names)
    pks = {k[1]: v for k, v in insp.get_multi_pk_constraint(
        schema=schema, filter_names=names).items()}
    fks = insp.get_multi_foreign_keys(schema=schema, filter_names=names)
    tables = [
        {
            "name": rel["name"],
            "type": rel["type"],
            "row_estimate": rel.get("row_estimate"),
            "size_bytes": rel.get("size_bytes"),
            "columns": len(columns.get(rel["name"], [])),
            "primary_key": list((pks.get(rel["name"]) or {}).get("constrained_columns") or []),
        }
        for rel in relations
    ]
    relationships = [
        {
            "from_table": key[1],
            "from_columns": fk.get("constrained_columns", []),
            "to_table": fk.get("referred_table"),
            "to_columns": fk.get("referred_columns", []),
        }
        for key, table_fks in sorted(fks.items(), key=lambda kv: kv[0][1])
        for fk in table_fks
    ]
    return {"tables": tables, "relationships": relationships, "source": "catalog"}


def search(
    insp: Inspector, schema: str | None, relations: list[dict[str, Any]], pattern: str
) -> dict[str, Any]:
    """Case-insensitive substring match over relation and column names."""
    needle = pattern.lower()
    columns = columns_by_table(insp, schema)
    matches: list[dict[str, Any]] = []
    truncated = False
    for rel in relations:
        name = rel["name"]
        candidates = []
        if needle in name.lower():
            candidates.append({"table": name, "column": None, "type": rel["type"],
                               "kind": "table"})
        candidates += [
            {"table": name, "column": c["name"], "type": str(c["type"]), "kind": "column"}
            for c in columns.get(name, [])
            if needle in c["name"].lower()
        ]
        room = MAX_SEARCH_MATCHES - len(matches)
        if len(candidates) > room:
            matches += candidates[:room]
            truncated = True
            break
        matches += candidates
    return {"pattern": pattern, "matches": matches, "truncated": truncated}


def _optional(fn: Callable[..., Any], table: str, schema: str | None, *, default: Any) -> Any:
    """Reflection calls some dialects do not implement."""
    try:
        return fn(table, schema=schema) or default
    except NotImplementedError:
        return default


def _as_int(value: Any) -> int | None:
    return None if value is None else int(value)
