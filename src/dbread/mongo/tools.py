"""MongoDB tool handlers — list/describe/query/explain for the `mongodb` dialect."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from .client import MongoClientManager
from .cost_guard import MongoCostGuard
from .guard import MongoGuard
from .schema import docs_to_rows, infer_schema, profile_docs

if TYPE_CHECKING:
    from ..audit import AuditLogger
    from ..connections import ConnectionManager
    from ..rate_limiter import RateLimiter


# Top-level command keys `_execute` actually consumes. Declared independently
# from the guard's COMMAND_FIELDS so that the equality test in
# tests/test_mongo_guard.py is a real check: a field accepted by the guard but
# never read here would be silently dropped, which is exactly the class of bug
# this mirror exists to catch.
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


def _optional(cmd: dict, *names: str) -> dict[str, Any]:
    """Collect the present optional keys, keeping MongoDB's own spelling."""
    return {name: cmd[name] for name in names if name in cmd}


def _to_server_command(cmd: dict) -> dict[str, Any]:
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
            _raise_tool_error("distinct_key_required")
        return {"distinct": cmd[name], **_rename_filter_to_query(rest)}
    return dict(cmd)


def _rename_filter_to_query(fields: dict) -> dict[str, Any]:
    return {("query" if k == "filter" else k): v for k, v in fields.items()}


def _raise_tool_error(msg: str) -> None:
    # Lazy import keeps tools.py ↔ mongo.tools import cycle collapsed.
    from ..tools import ToolError

    raise ToolError(msg)


class MongoToolHandlers:
    def __init__(
        self,
        conn_mgr: ConnectionManager,
        mongo_mgr: MongoClientManager,
        rate_limiter: RateLimiter,
        audit: AuditLogger,
        guard: MongoGuard | None = None,
        cost_guard: MongoCostGuard | None = None,
    ) -> None:
        self.conn_mgr = conn_mgr
        self.mc = mongo_mgr
        self.rl = rate_limiter
        self.audit = audit
        self.guard = guard or MongoGuard()
        self.cost_guard = cost_guard or MongoCostGuard()

    # --- schema introspection ---------------------------------------------

    def list_tables(self, connection: str) -> list[str]:
        db = self.mc.get_db(connection)
        return sorted(db.list_collection_names())

    def list_schemas(self, connection: str) -> list[str]:
        """The one database this connection is pinned to."""
        return [self.mc.get_db(connection).name]

    def describe_table(self, connection: str, table: str) -> dict[str, Any]:
        cfg = self.conn_mgr.get_config(connection)
        size = cfg.mongo.sample_size if cfg.mongo else 100
        coll = self.mc.get_db(connection)[table]
        sample = list(coll.aggregate([{"$sample": {"size": size}}]))
        indexes = [
            {"name": i.get("name"),
             "keys": list(i["key"].items()) if "key" in i else [],
             "unique": bool(i.get("unique", False))}
            for i in coll.list_indexes()
        ]
        return {"fields": infer_schema(sample, size), "indexes": indexes,
                "sample_size": len(sample), "source": "sampled"}

    def profile_table(
        self,
        connection: str,
        table: str,
        columns: list[str] | None = None,
        sample_size: int | None = None,
    ) -> dict[str, Any]:
        cfg = self.conn_mgr.get_config(connection)
        default = cfg.mongo.sample_size if cfg.mongo else 100
        size = sample_size if sample_size and sample_size > 0 else default
        # Routed through `query` so the sample draw is guarded, rate-limited
        # and audited exactly like any other aggregate.
        out = self.query(
            connection,
            {"aggregate": table, "pipeline": [{"$sample": {"size": size}}]},
            max_rows=size,
        )
        docs = [dict(zip(out["columns"], row, strict=False)) for row in out["rows"]]
        return {
            "table": table,
            "sampled_rows": len(docs),
            "source": "sampled",
            "fields": profile_docs(docs, columns),
        }

    # --- query + explain ---------------------------------------------------

    def query(
        self,
        connection: str,
        command: dict,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        cfg = self.conn_mgr.get_config(connection)
        cap = (
            max_rows
            if max_rows is not None and 0 < max_rows <= cfg.max_rows
            else cfg.max_rows
        )

        cmd = dict(command)
        cmd["maxTimeMS"] = max(1, cfg.statement_timeout_s) * 1000

        result = self.guard.validate_command(cmd)
        if not result.allowed:
            self._audit(connection, cmd, "rejected", reason=result.reason)
            _raise_tool_error(f"mongo_guard: {result.reason}")

        cmd = self.guard.inject_limit(cmd, cap)

        db = self.mc.get_db(connection)
        cost_ms: int | None = None
        if cfg.max_rows_estimate is not None:
            # Use the original command (pre-limit) so the planner reports true
            # collection-scan cost — injected limits would mask full scans.
            docs_est, cost_ms = self.cost_guard.estimate_docs(
                db, command, cfg.max_rows_estimate
            )
            if docs_est is not None and docs_est > cfg.max_rows_estimate:
                reason = (
                    f"cost_guard_error: docs_estimate={docs_est} "
                    f"exceeds {cfg.max_rows_estimate}"
                )
                self._audit(connection, cmd, "rejected", reason=reason, cost_check_ms=cost_ms)
                _raise_tool_error(reason)

        granted, scope = self.rl.acquire_with_reason(connection)
        if not granted:
            reason = f"rate_limit_{scope}" if scope else "rate_limit"
            self._audit(connection, cmd, "rejected", reason=reason, cost_check_ms=cost_ms)
            _raise_tool_error(
                f"rate_limit_exceeded: {scope}" if scope else "rate_limit_exceeded"
            )

        t0 = time.perf_counter()
        try:
            rows, columns = self._execute(db, cmd, cap)
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            self._audit(
                connection, cmd, "failed", ms=ms, reason=str(e)[:200],
                cost_check_ms=cost_ms,
            )
            _raise_tool_error(f"db_error: {e}")

        ms = int((time.perf_counter() - t0) * 1000)
        self._audit(connection, cmd, "ok", rows=len(rows), ms=ms, cost_check_ms=cost_ms)
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(rows) == cap,
        }

    def explain(self, connection: str, command: dict) -> dict[str, Any]:
        cfg = self.conn_mgr.get_config(connection)
        cmd = dict(command)
        cmd["maxTimeMS"] = max(1, cfg.statement_timeout_s) * 1000

        result = self.guard.validate_command(cmd)
        if not result.allowed:
            self._audit(connection, cmd, "rejected", reason=result.reason)
            _raise_tool_error(f"mongo_guard: {result.reason}")

        granted, scope = self.rl.acquire_with_reason(connection)
        if not granted:
            reason = f"rate_limit_{scope}" if scope else "rate_limit"
            self._audit(connection, cmd, "rejected", reason=reason)
            _raise_tool_error(
                f"rate_limit_exceeded: {scope}" if scope else "rate_limit_exceeded"
            )

        db = self.mc.get_db(connection)
        t0 = time.perf_counter()
        try:
            plan = db.command(
                "explain", _to_server_command(cmd), verbosity="queryPlanner"
            )
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            self._audit(connection, cmd, "failed", ms=ms, reason=str(e)[:200])
            _raise_tool_error(f"db_error: {e}")

        ms = int((time.perf_counter() - t0) * 1000)
        self._audit(connection, cmd, "ok", ms=ms)
        return {"plan": plan}

    # --- internals ---------------------------------------------------------

    def _execute(self, db: Any, cmd: dict, cap: int) -> tuple[list[list], list[str]]:
        name = next(iter(cmd))
        coll_name = cmd[name]
        coll = db[coll_name]
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
            n = coll.estimated_document_count(
                maxTimeMS=max_time_ms, **_optional(cmd, "comment")
            )
            return [[n]], ["count"]

        if name == "distinct":
            key = cmd.get("key")
            if not isinstance(key, str):
                _raise_tool_error("distinct_key_required")
            values = coll.distinct(
                key,
                cmd.get("filter", {}),
                maxTimeMS=max_time_ms,
                **_optional(cmd, "collation", "comment"),
            )[:cap]
            return [[v] for v in values], [key]

        _raise_tool_error(f"internal_unexpected_command: {name}")
        return [], []  # unreachable, keeps type-checker happy

    def _audit(
        self, connection: str, cmd: dict, status: str, *,
        rows: int = 0, ms: int = 0, reason: str | None = None,
        cost_check_ms: int | None = None,
    ) -> None:
        to_log = cmd
        if getattr(self.audit, "redact_literals", False):
            from ..audit import redact_mongo_command
            to_log = redact_mongo_command(cmd)
        self.audit.log(
            connection,
            json.dumps(to_log, default=str, ensure_ascii=False),
            status, rows=rows, ms=ms, reason=reason, dialect="mongodb",
            cost_check_ms=cost_check_ms,
        )
