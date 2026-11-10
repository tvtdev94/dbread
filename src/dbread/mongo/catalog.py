"""Whole-database views: overview, schema search, schema snapshot.

Mongo declares no schema, so every column-level answer here comes from a
`$sample` drawn through `query` (guard, cost guard, rate limit, audit). Each
tool spends one rate-limit token on listCollections and one per read per
collection, so every tool caps how many collections it reads and says
`truncated` when it stopped early.
"""

from __future__ import annotations

from typing import Any

from .execute import raise_tool_error
from .inspect import collection_infos, index_infos, stats_pipeline, sum_storage_stats
from .query import MongoQueryMixin
from .schema import field_summary

# Documents sampled per collection by the overview and the schema search.
# Small on purpose: these tools touch every collection.
BROAD_SAMPLE_SIZE = 20
# Collections whose fields `search_schema` samples; names are matched for all.
SEARCH_MAX_COLLECTIONS = 25
SEARCH_MAX_MATCHES = 200
# Collections `database_overview` and `schema_snapshot` read per call, first
# by name. Each costs one or two rate-limit tokens; uncapped, a large database
# would drain the connection's whole per-minute budget in one call.
MAX_COLLECTIONS = 20


class MongoCatalogMixin(MongoQueryMixin):
    def _collections(self, connection: str, database: str | None) -> tuple[Any, list[dict]]:
        op = {"listCollections": 1}
        db = self._resolve(connection, database, op)
        ms = self._max_time_ms(connection)
        infos = self._metadata(
            connection, op, lambda: collection_infos(db, ms),
            db_name=self._explicit(database, db),
        )
        return db, infos

    def database_overview(
        self, connection: str, database: str | None = None,
    ) -> dict[str, Any]:
        db, infos = self._collections(connection, database)
        tables: list[dict[str, Any]] = []
        for info in infos[:MAX_COLLECTIONS]:
            entry = self._overview_entry(connection, info, database)
            # Once the budget is spent every further read is refused too, so
            # stop instead of sending (and auditing) one refusal per collection.
            if entry.get("error", "").startswith("rate_limit_exceeded"):
                break
            tables.append(entry)
        return {
            "connection": connection,
            "schema": db.name,
            "tables": tables,
            "relationships": [],  # Mongo declares no references to report
            "source": "sampled",
            "truncated": len(tables) < len(infos),
        }

    def _overview_entry(
        self, connection: str, info: dict, database: str | None,
    ) -> dict[str, Any]:
        """One collection's row; a read that fails nulls its numbers and says why."""
        from ..tools import ToolError

        name, kind = info["name"], info["type"]
        entry: dict[str, Any] = {
            "name": name, "type": kind, "row_estimate": None, "size_bytes": None,
            "columns": None, "primary_key": [] if kind == "view" else ["_id"],
        }
        try:
            docs = self._sample_docs(connection, name, BROAD_SAMPLE_SIZE, database)
            # Views have no storage of their own, so they are only sampled.
            stats = None if kind == "view" else sum_storage_stats(
                self._aggregate_docs(connection, name, stats_pipeline(), database)
            )
        except ToolError as e:
            return {**entry, "error": str(e)}
        entry["columns"] = len(field_summary(docs))
        if stats is not None:
            totals = stats["totals"]
            entry["row_estimate"] = totals["count"]
            if totals["storage_bytes"] is not None:
                entry["size_bytes"] = totals["storage_bytes"] + sum(stats["index_sizes"].values())
        return entry

    def search_schema(
        self, connection: str, pattern: str, database: str | None = None,
    ) -> dict[str, Any]:
        """Case-insensitive substring match over collection names and sampled fields."""
        if not isinstance(pattern, str) or not pattern.strip():
            raise_tool_error("invalid_input: pattern must be a non-empty string")
        needle = pattern.strip().lower()
        _, infos = self._collections(connection, database)
        matches: list[dict[str, Any]] = [
            {"table": c["name"], "column": None, "type": None, "kind": "table"}
            for c in infos if needle in c["name"].lower()
        ]
        # Unsampled collections may hold matching fields we never saw.
        truncated = len(infos) > SEARCH_MAX_COLLECTIONS
        for c in infos[:SEARCH_MAX_COLLECTIONS]:
            if len(matches) > SEARCH_MAX_MATCHES:
                break
            docs = self._sample_docs(connection, c["name"], BROAD_SAMPLE_SIZE, database)
            matches += [
                {"table": c["name"], "column": path, "type": f["type"], "kind": "column"}
                for path, f in field_summary(docs).items() if needle in path.lower()
            ]
        if len(matches) > SEARCH_MAX_MATCHES:
            matches, truncated = matches[:SEARCH_MAX_MATCHES], True
        return {"pattern": pattern, "matches": matches, "truncated": truncated}

    def schema_snapshot(
        self, connection: str, database: str | None = None,
    ) -> dict[str, Any]:
        """Sampled field types + declared indexes for every collection."""
        op = {"listCollections": 1, "listIndexes": "*"}
        db = self._resolve(connection, database, op)
        ms = self._max_time_ms(connection)

        def read() -> tuple[bool, list[tuple[dict, list[dict]]]]:
            infos = collection_infos(db, ms)
            return len(infos) > MAX_COLLECTIONS, [
                (c, index_infos(db, c["name"], c["type"], ms)) for c in infos[:MAX_COLLECTIONS]
            ]

        truncated, catalog = self._metadata(
            connection, op, read, db_name=self._explicit(database, db),
        )
        size = self._sample_size(connection)
        tables: dict[str, Any] = {}
        for info, indexes in catalog:
            docs = self._sample_docs(connection, info["name"], size, database)
            tables[info["name"]] = {
                "type": info["type"],
                "columns": field_summary(docs),
                "indexes": {
                    i["name"]: {"columns": [k for k, _ in i["keys"]], "unique": i["unique"]}
                    for i in indexes
                },
            }
        return {"tables": tables, "truncated": truncated}
