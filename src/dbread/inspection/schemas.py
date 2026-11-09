"""MCP tool schemas for the whole-database inspection tools."""

from __future__ import annotations

from typing import Any

from mcp.types import Tool

DATABASE_PROPERTY: dict[str, Any] = {
    "type": "string",
    "description": (
        "MongoDB only: database to read, from the connection's allowed set "
        "(URI database plus mongo.databases). Omit for the URI database. "
        "SQL connections reject it."
    ),
}

_SCHEMA_PROPERTY = {"type": "string", "description": "SQL only: schema to inspect"}


def _object(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required}


def inspection_tool_schemas() -> list[Tool]:
    return [
        Tool(
            name="list_databases",
            description=(
                "MongoDB only: databases this connection may read, with "
                "size_bytes and an empty flag. SQL connections reject it."
            ),
            inputSchema=_object({"connection": {"type": "string"}}, ["connection"]),
        ),
        Tool(
            name="collection_stats",
            description=(
                "MongoDB only: document count, data/storage size, average "
                "document size, and per-index size and usage (ops since)."
            ),
            inputSchema=_object(
                {
                    "connection": {"type": "string"},
                    "table": {"type": "string", "description": "Collection name"},
                    "database": DATABASE_PROPERTY,
                },
                ["connection", "table"],
            ),
        ),
        Tool(
            name="database_overview",
            description=(
                "One-call map of a database: every table/view/collection with "
                "row estimate, size, column count and primary key, plus the "
                "declared foreign-key relationships (SQL; always empty on "
                "MongoDB). Start here on an unfamiliar database."
            ),
            inputSchema=_object(
                {
                    "connection": {"type": "string"},
                    "schema": _SCHEMA_PROPERTY,
                    "database": DATABASE_PROPERTY,
                },
                ["connection"],
            ),
        ),
        Tool(
            name="search_schema",
            description=(
                "Find tables and columns whose name contains `pattern` "
                "(case-insensitive substring). Returns at most 200 matches "
                "and sets truncated when there were more."
            ),
            inputSchema=_object(
                {
                    "connection": {"type": "string"},
                    "pattern": {"type": "string"},
                    "schema": _SCHEMA_PROPERTY,
                    "database": DATABASE_PROPERTY,
                },
                ["connection", "pattern"],
            ),
        ),
        Tool(
            name="check_integrity",
            description=(
                "Exact whole-table data checks, each run as guarded, audited "
                "queries: orphans (rows whose foreign key points at no parent; "
                "SQL uses declared foreign keys, MongoDB uses `references`), "
                "duplicates over `unique_columns` (NULLs excluded), "
                "null_columns (NULL on every row) and constant_columns (one "
                "distinct non-null value). Each check reports a violation "
                "count, up to 5 examples, and its own error without stopping "
                "the others. Can be slow on large tables."
            ),
            inputSchema=_object(
                {
                    "connection": {"type": "string"},
                    "table": {"type": "string"},
                    "checks": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "orphans", "duplicates", "null_columns",
                                "constant_columns",
                            ],
                        },
                        "description": "Subset to run; omit for every applicable check",
                    },
                    "references": {
                        "type": "array",
                        "description": (
                            "MongoDB only: [{field, to, to_field}] with to_field "
                            "defaulting to _id. SQL connections reject it."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string"},
                                "to": {"type": "string"},
                                "to_field": {"type": "string"},
                            },
                            "required": ["field", "to"],
                        },
                    },
                    "unique_columns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Columns that should be unique together; "
                                       "required for the duplicates check",
                    },
                    "schema": _SCHEMA_PROPERTY,
                    "database": DATABASE_PROPERTY,
                },
                ["connection", "table"],
            ),
        ),
        Tool(
            name="diff_schema",
            description=(
                "Compare the schema of two connections (or two schemas / "
                "databases of one): tables only on one side, and per table the "
                "columns and indexes added, removed or changed."
            ),
            inputSchema=_object(
                {
                    "connection_a": {"type": "string"},
                    "connection_b": {"type": "string"},
                    "schema_a": _SCHEMA_PROPERTY,
                    "schema_b": _SCHEMA_PROPERTY,
                    "database_a": DATABASE_PROPERTY,
                    "database_b": DATABASE_PROPERTY,
                },
                ["connection_a", "connection_b"],
            ),
        ),
        Tool(
            name="db_health",
            description=(
                "PostgreSQL / MySQL server health: top statements, unused "
                "indexes, lock waits, plus dead tuples and long-running "
                "transactions (PostgreSQL) or fragmentation (MySQL). A section "
                "the user lacks privileges for returns its error and the exact "
                "GRANT or extension that fixes it; the other sections still run."
            ),
            inputSchema=_object({"connection": {"type": "string"}}, ["connection"]),
        ),
    ]
