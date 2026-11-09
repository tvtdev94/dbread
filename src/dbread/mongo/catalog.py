"""Whole-database views: overview, schema search, schema snapshot.

Mongo declares no schema, so every column-level answer here comes from a
`$sample` drawn through `query` (guard, cost guard, rate limit, audit). Each
tool spends one rate-limit token on listCollections and one per collection it
samples; `search_schema` caps how many collections it samples to keep that
bounded.
"""

from __future__ import annotations

from typing import Any

from .execute import raise_tool_error
from .inspect import STATS_PROJECTION, collection_infos, index_infos, sum_storage_stats
from .query import MongoQueryMixin
from .schema import field_summary

# Documents sampled per collection by the overview and the schema search.
# Small on purpose: these tools touch every collection.
BROAD_SAMPLE_SIZE = 20
# Collections whose fields `search_schema` samples; names are matched for all.
SEARCH_MAX_COLLECTIONS = 25
SEARCH_MAX_MATCHES = 200


def _wrapped_sample(size: int) -> list[dict]:
    return [{"$sample": {"size": size}}, {"$replaceWith": {"doc": "$$ROOT"}}]


class MongoCatalogMixin(MongoQueryMixin):
    def _collections(self, connection: str, database: str | None) -> tuple[Any, list[dict]]:
        op = {"listCollections": 1}
        db = self._resolve(connection, database, op)
        infos = self._metadata(
            connection, op, lambda: collection_infos(db),
            db_name=self._explicit(database, db),
        )
        return db, infos

    def database_overview(
        self, connection: str, database: str | None = None,
    ) -> dict[str, Any]:
        db, infos = self._collections(connection, database)
        return {
            "connection": connection,
            "schema": db.name,
            "tables": [self._overview_entry(connection, c, database) for c in infos],
            "relationships": [],  # Mongo declares no references to report
            "source": "sampled",
        }

    def _overview_entry(
        self, connection: str, info: dict, database: str | None,
    ) -> dict[str, Any]:
        name, kind = info["name"], info["type"]
        sample = _wrapped_sample(BROAD_SAMPLE_SIZE)
        # Stats and sample ride one aggregate so each collection costs one
        # query. Views have no storage of their own, so they only sample.
        pipeline = sample if kind == "view" else [
            {"$collStats": {"storageStats": {}}},
            {"$project": STATS_PROJECTION},
            {"$unionWith": {"coll": name, "pipeline": sample}},
        ]
        rows = self._aggregate_docs(
            connection, name, pipeline, database, max_rows=BROAD_SAMPLE_SIZE + 64,
        )
        docs = [r["doc"] for r in rows if isinstance(r.get("doc"), dict)]
        stats = sum_storage_stats([r for r in rows if "doc" not in r])
        totals = stats["totals"]
        on_disk = (
            totals["storage_bytes"] + sum(stats["index_sizes"].values())
            if totals["storage_bytes"] is not None else None
        )
        return {
            "name": name,
            "type": kind,
            "row_estimate": totals["count"],
            "size_bytes": on_disk,
            "columns": len(field_summary(docs)),
            "primary_key": [] if kind == "view" else ["_id"],
        }

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
        catalog = self._metadata(
            connection, op,
            lambda: [(c, index_infos(db, c["name"], c["type"])) for c in collection_infos(db)],
            db_name=self._explicit(database, db),
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
        return {"tables": tables}
