"""MongoDB tool handlers — list/describe/query/explain for the `mongodb` dialect."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from .client import MongoClientManager
from .guard import MongoGuard
from .schema import docs_to_rows, infer_schema

if TYPE_CHECKING:
    from ..audit import AuditLogger
    from ..connections import ConnectionManager
    from ..rate_limiter import RateLimiter


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
    ) -> None:
        self.conn_mgr = conn_mgr
        self.mc = mongo_mgr
        self.rl = rate_limiter
        self.audit = audit
        self.guard = guard or MongoGuard()

    # --- schema introspection ---------------------------------------------

    def list_tables(self, connection: str) -> list[str]:
        db = self.mc.get_db(connection)
        return sorted(db.list_collection_names())

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
            rows, columns = self._execute(db, cmd, cap)
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            self._audit(connection, cmd, "failed", ms=ms, reason=str(e)[:200])
            _raise_tool_error(f"db_error: {e}")

        ms = int((time.perf_counter() - t0) * 1000)
        self._audit(connection, cmd, "ok", rows=len(rows), ms=ms)
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
            plan = db.command("explain", cmd, verbosity="queryPlanner")
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
            cursor = coll.find(
                cmd.get("filter", {}),
                cmd.get("projection"),
            ).limit(int(cmd.get("limit", cap))).max_time_ms(max_time_ms)
            return docs_to_rows(list(cursor), cap)

        if name == "aggregate":
            cursor = coll.aggregate(
                list(cmd.get("pipeline", [])),
                maxTimeMS=max_time_ms,
            )
            return docs_to_rows(list(cursor), cap)

        if name in ("count", "countDocuments"):
            n = coll.count_documents(cmd.get("filter", {}), maxTimeMS=max_time_ms)
            return [[n]], ["count"]

        if name == "estimatedDocumentCount":
            n = coll.estimated_document_count(maxTimeMS=max_time_ms)
            return [[n]], ["count"]

        if name == "distinct":
            key = cmd.get("key")
            if not isinstance(key, str):
                _raise_tool_error("distinct_key_required")
            values = coll.distinct(key, cmd.get("filter", {}))[:cap]
            return [[v] for v in values], [key]

        _raise_tool_error(f"internal_unexpected_command: {name}")
        return [], []  # unreachable, keeps type-checker happy

    def _audit(
        self, connection: str, cmd: dict, status: str, *,
        rows: int = 0, ms: int = 0, reason: str | None = None,
    ) -> None:
        to_log = cmd
        if getattr(self.audit, "redact_literals", False):
            from ..audit import redact_mongo_command
            to_log = redact_mongo_command(cmd)
        self.audit.log(
            connection,
            json.dumps(to_log, default=str, ensure_ascii=False),
            status, rows=rows, ms=ms, reason=reason, dialect="mongodb",
        )
