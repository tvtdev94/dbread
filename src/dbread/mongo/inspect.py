"""Per-collection and per-database introspection tools."""

from __future__ import annotations

from typing import Any

from .execute import raise_tool_error
from .query import MongoQueryMixin
from .schema import infer_schema, profile_docs


def collection_infos(
    db: Any, max_time_ms: int, name: str | None = None,
) -> list[dict[str, Any]]:
    """listCollections as `[{"name","type","options"}]`, sorted, minus `system.*`.

    `system.*` namespaces are server bookkeeping (view definitions, bucket
    storage), not data an agent should be pointed at.
    """
    found = db.list_collections(
        filter={"name": name} if name else None, maxTimeMS=max_time_ms,
    )
    infos = [
        {"name": c["name"], "type": c.get("type", "collection"),
         "options": c.get("options") or {}}
        for c in found
        if not c["name"].startswith("system.")
    ]
    return sorted(infos, key=lambda c: c["name"])


def index_infos(db: Any, table: str, kind: str) -> list[dict[str, Any]]:
    """listIndexes for a collection; views have none and refuse the command."""
    if kind == "view":
        return []
    return [
        {"name": i.get("name"),
         "keys": list(i["key"].items()) if "key" in i else [],
         "unique": bool(i.get("unique", False))}
        for i in db[table].list_indexes()
    ]


class MongoInspectMixin(MongoQueryMixin):
    def list_tables(
        self, connection: str, database: str | None = None,
    ) -> list[dict[str, str]]:
        op = {"listCollections": 1}
        db = self._resolve(connection, database, op)
        ms = self._max_time_ms(connection)
        infos = self._metadata(
            connection, op, lambda: collection_infos(db, ms),
            db_name=self._explicit(database, db),
        )
        return [{"name": c["name"], "type": c["type"]} for c in infos]

    def list_schemas(self, connection: str, database: str | None = None) -> list[str]:
        """Every database this connection may read (see `mongo.databases`)."""
        op = {"listDatabases": 1}
        self._resolve(connection, database, op)
        return self._metadata(connection, op, lambda: self.allowed_databases(connection))

    def list_databases(
        self, connection: str, database: str | None = None,
    ) -> list[dict[str, Any]]:
        op = {"listDatabases": 1}
        self._resolve(connection, database, op)

        def read() -> list[dict[str, Any]]:
            client = self.mc.get_client(connection)
            ms = self._max_time_ms(connection)
            listed = {d["name"]: d for d in client.list_databases(maxTimeMS=ms)}
            out = []
            for name in self.allowed_databases(connection):
                info = listed.get(name)
                # An allowlisted database the server does not report either
                # does not exist yet or is hidden from this user: say unknown.
                out.append({
                    "name": name,
                    "size_bytes": int(info["sizeOnDisk"]) if info and "sizeOnDisk" in info
                    else None,
                    "empty": bool(info.get("empty", False)) if info else None,
                })
            return out

        return self._metadata(connection, op, read)

    def describe_table(
        self, connection: str, table: str, database: str | None = None,
    ) -> dict[str, Any]:
        op = {"listCollections": table, "listIndexes": table}
        db = self._resolve(connection, database, op)

        def read() -> tuple[list[dict], list[dict]]:
            infos = collection_infos(db, self._max_time_ms(connection), table)
            kind = infos[0]["type"] if infos else "collection"
            return infos, (index_infos(db, table, kind) if infos else [])

        infos, indexes = self._metadata(
            connection, op, read, db_name=self._explicit(database, db),
        )
        if not infos:
            raise_tool_error(f"unknown_table: {table}")
        size = self._sample_size(connection)
        sample = self._sample_docs(connection, table, size, database)
        return {
            "type": infos[0]["type"],
            "fields": infer_schema(sample, size),
            "indexes": indexes,
            "validator": infos[0]["options"].get("validator"),
            "sample_size": len(sample),
            "source": "sampled",
        }

    def profile_table(
        self,
        connection: str,
        table: str,
        columns: list[str] | None = None,
        sample_size: int | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        # The connection's configured sample size is the default; $sample
        # switches to a blocking in-memory sort once it reaches 5% of the
        # collection, so asking for more than configured is not free.
        default = self._sample_size(connection)
        size = sample_size if sample_size and sample_size > 0 else default
        docs = self._sample_docs(connection, table, size, database)
        try:
            fields = profile_docs(docs, columns)
        except ValueError as e:
            raise_tool_error(f"invalid_input: {e}")
        return {
            "table": table,
            "sampled_rows": len(docs),
            "sample_size": size,
            "source": "sampled",
            "fields": fields,
        }

    def collection_stats(
        self, connection: str, table: str, database: str | None = None,
    ) -> dict[str, Any]:
        """Storage size and per-index size + usage, via `$collStats` / `$indexStats`."""
        from ..tools import ToolError

        stats = self._storage_stats(connection, table, database)
        out: dict[str, Any] = {"table": table, **stats["totals"]}
        usage: dict[str, dict[str, Any]] = {}
        try:
            for doc in self._aggregate_docs(connection, table, [
                {"$indexStats": {}},
                {"$project": {"_id": 0, "name": 1, "ops": "$accesses.ops",
                              "since": "$accesses.since"}},
            ], database):
                seen = usage.setdefault(doc["name"], {"ops": 0, "since": None})
                # A sharded collection reports one row per shard.
                seen["ops"] += int(doc.get("ops") or 0)
                since = doc.get("since")
                if since is not None and (seen["since"] is None or since < seen["since"]):
                    seen["since"] = since
        except ToolError as e:
            # $indexStats needs the `indexStats` action, which the plain
            # `read` role lacks; sizes are still worth returning without it.
            # Rate-limit and cost-guard refusals are not that, so they raise.
            if not str(e).startswith("db_error"):
                raise
            out["index_usage_error"] = str(e)
        out["indexes"] = [
            {"name": name, "size_bytes": size,
             "ops": usage.get(name, {}).get("ops"),
             "since": usage.get(name, {}).get("since")}
            for name, size in sorted(stats["index_sizes"].items())
        ]
        return out

    def _storage_stats(
        self, connection: str, table: str, database: str | None,
    ) -> dict[str, Any]:
        return sum_storage_stats(
            self._aggregate_docs(connection, table, stats_pipeline(), database)
        )


STATS_PROJECTION = {
    "_id": 0,
    "count": "$storageStats.count",
    "size": "$storageStats.size",
    "storage": "$storageStats.storageSize",
    "index_sizes": "$storageStats.indexSizes",
}


def stats_pipeline() -> list[dict]:
    """`$collStats` storage numbers, one row per shard."""
    return [{"$collStats": {"storageStats": {}}}, {"$project": STATS_PROJECTION}]


def sum_storage_stats(docs: list[dict]) -> dict[str, Any]:
    """Fold `$collStats` rows (one per shard) into collection totals."""
    count = sum(int(d.get("count") or 0) for d in docs)
    size = sum(int(d.get("size") or 0) for d in docs)
    storage = sum(int(d.get("storage") or 0) for d in docs)
    index_sizes: dict[str, int] = {}
    for d in docs:
        for name, n in (d.get("index_sizes") or {}).items():
            index_sizes[name] = index_sizes.get(name, 0) + int(n)
    return {
        "totals": {
            "count": count if docs else None,
            "size_bytes": size if docs else None,
            "storage_bytes": storage if docs else None,
            "avg_doc_bytes": round(size / count) if count else None,
        },
        "index_sizes": index_sizes,
    }
