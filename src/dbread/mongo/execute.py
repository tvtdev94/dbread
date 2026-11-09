"""Translate a validated dbread command spec into pymongo calls.

Pure functions over a pymongo `Database`; no guard, rate limit or audit here —
callers in `query.py` own those.
"""

from __future__ import annotations

from typing import Any, NoReturn

from .schema import docs_to_rows

# Top-level command keys `execute` consumes, declared separately from the
# guard's COMMAND_FIELDS so the equality test catches the two drifting apart.
# It is a cheap tripwire, not a proof: the per-field tests in
# tests/test_mongo_tools.py are what show an option reaches the driver.
HANDLED_FIELDS: dict[str, frozenset[str]] = {
    "find": frozenset({
        "find", "filter", "projection", "sort", "skip", "limit",
        "hint", "collation", "batchSize", "comment", "maxTimeMS",
    }),
    "aggregate": frozenset({
        "aggregate", "pipeline", "collation", "let", "hint",
        "allowDiskUse", "comment", "maxTimeMS",
    }),
    "count": frozenset({
        "count", "filter", "skip", "limit", "hint", "collation",
        "comment", "maxTimeMS",
    }),
    "countDocuments": frozenset({
        "countDocuments", "filter", "skip", "limit", "hint", "collation",
        "comment", "maxTimeMS",
    }),
    "estimatedDocumentCount": frozenset({
        "estimatedDocumentCount", "comment", "maxTimeMS",
    }),
    "distinct": frozenset({
        "distinct", "key", "filter", "collation", "comment", "maxTimeMS",
    }),
}


def raise_tool_error(msg: str) -> NoReturn:
    # Lazy import keeps the tools.py <-> mongo import cycle collapsed.
    from ..tools import ToolError

    raise ToolError(msg)


def _optional(cmd: dict, *names: str) -> dict[str, Any]:
    """Collect the present optional keys, keeping MongoDB's own spelling."""
    return {name: cmd[name] for name in names if name in cmd}


def _rename_filter_to_query(fields: dict) -> dict[str, Any]:
    return {("query" if k == "filter" else k): v for k, v in fields.items()}


def to_server_command(cmd: dict) -> dict[str, Any]:
    """Translate dbread's command spec into the MongoDB wire command.

    `query` runs through pymongo helpers that name the predicate `filter`,
    but `explain` speaks the wire protocol directly, where count and distinct
    call that same thing `query`, aggregate refuses to run without a `cursor`
    document, and estimatedDocumentCount is a driver-only helper the server
    has never heard of.
    """
    name = next(iter(cmd))
    rest = {k: v for k, v in cmd.items() if k != name}

    if name == "aggregate":
        return {**cmd, "cursor": {}}
    if name == "estimatedDocumentCount":
        return {"count": cmd[name], **_optional(rest, "comment", "maxTimeMS")}
    if name in ("count", "countDocuments"):
        return {"count": cmd[name], **_rename_filter_to_query(rest)}
    if name == "distinct":
        if not isinstance(cmd.get("key"), str):
            raise_tool_error("distinct_key_required")
        return {"distinct": cmd[name], **_rename_filter_to_query(rest)}
    return dict(cmd)


def execute(db: Any, cmd: dict, cap: int) -> tuple[list[list], list[str]]:
    """Run an already-guarded, limit-injected command; return (rows, columns)."""
    name = next(iter(cmd))
    coll = db[cmd[name]]
    max_time_ms = cmd["maxTimeMS"]

    if name == "find":
        cursor = coll.find(cmd.get("filter", {}), cmd.get("projection"))
        # Chained in MongoDB's own order-independent option style; each is
        # applied only when the caller asked for it so cursor defaults win.
        if "sort" in cmd:
            cursor = cursor.sort(cmd["sort"])
        if "skip" in cmd:
            cursor = cursor.skip(cmd["skip"])
        if "hint" in cmd:
            cursor = cursor.hint(cmd["hint"])
        if "collation" in cmd:
            cursor = cursor.collation(cmd["collation"])
        if "batchSize" in cmd:
            cursor = cursor.batch_size(cmd["batchSize"])
        if "comment" in cmd:
            cursor = cursor.comment(cmd["comment"])
        cursor = cursor.limit(int(cmd.get("limit", cap))).max_time_ms(max_time_ms)
        return docs_to_rows(list(cursor), cap)

    if name == "aggregate":
        cursor = coll.aggregate(
            list(cmd.get("pipeline", [])),
            maxTimeMS=max_time_ms,
            **_optional(cmd, "collation", "let", "hint", "allowDiskUse", "comment"),
        )
        return docs_to_rows(list(cursor), cap)

    if name in ("count", "countDocuments"):
        n = coll.count_documents(
            cmd.get("filter", {}),
            maxTimeMS=max_time_ms,
            **_optional(cmd, "skip", "limit", "hint", "collation", "comment"),
        )
        return [[n]], ["count"]

    if name == "estimatedDocumentCount":
        n = coll.estimated_document_count(maxTimeMS=max_time_ms, **_optional(cmd, "comment"))
        return [[n]], ["count"]

    if name == "distinct":
        key = cmd.get("key")
        if not isinstance(key, str):
            raise_tool_error("distinct_key_required")
        values = coll.distinct(
            key,
            cmd.get("filter", {}),
            maxTimeMS=max_time_ms,
            **_optional(cmd, "collation", "comment"),
        )[:cap]
        return [[v] for v in values], [key]

    raise_tool_error(f"internal_unexpected_command: {name}")
