"""Guarded query + explain — the single path every Mongo data read takes."""

from __future__ import annotations

import time
from typing import Any

from .base import MongoBase
from .execute import execute, raise_tool_error, to_server_command


class MongoQueryMixin(MongoBase):
    def query(
        self,
        connection: str,
        command: dict,
        max_rows: int | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        cfg = self.conn_mgr.get_config(connection)
        cap = (
            max_rows
            if max_rows is not None and 0 < max_rows <= cfg.max_rows
            else cfg.max_rows
        )

        cmd = dict(command)
        cmd["maxTimeMS"] = max(1, cfg.statement_timeout_s) * 1000
        db = self._resolve(connection, database, cmd)
        db_name = self._explicit(database, db)

        result = self.guard.validate_command(cmd)
        if not result.allowed:
            self._audit(connection, cmd, "rejected", reason=result.reason, db_name=db_name)
            raise_tool_error(f"mongo_guard: {result.reason}")

        cmd = self.guard.inject_limit(cmd, cap)

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
                self._audit(
                    connection, cmd, "rejected", reason=reason,
                    cost_check_ms=cost_ms, db_name=db_name,
                )
                raise_tool_error(reason)

        self._acquire(connection, cmd, cost_check_ms=cost_ms, db_name=db_name)

        t0 = time.perf_counter()
        try:
            rows, columns = execute(db, cmd, cap)
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            self._audit(
                connection, cmd, "failed", ms=ms, reason=str(e)[:200],
                cost_check_ms=cost_ms, db_name=db_name,
            )
            raise_tool_error(f"db_error: {e}")

        ms = int((time.perf_counter() - t0) * 1000)
        self._audit(
            connection, cmd, "ok", rows=len(rows), ms=ms,
            cost_check_ms=cost_ms, db_name=db_name,
        )
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(rows) == cap,
        }

    def explain(
        self, connection: str, command: dict, database: str | None = None,
    ) -> dict[str, Any]:
        cfg = self.conn_mgr.get_config(connection)
        cmd = dict(command)
        cmd["maxTimeMS"] = max(1, cfg.statement_timeout_s) * 1000
        db = self._resolve(connection, database, cmd)
        db_name = self._explicit(database, db)

        result = self.guard.validate_command(cmd)
        if not result.allowed:
            self._audit(connection, cmd, "rejected", reason=result.reason, db_name=db_name)
            raise_tool_error(f"mongo_guard: {result.reason}")

        self._acquire(connection, cmd, db_name=db_name)

        t0 = time.perf_counter()
        try:
            plan = db.command("explain", to_server_command(cmd), verbosity="queryPlanner")
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            self._audit(connection, cmd, "failed", ms=ms, reason=str(e)[:200], db_name=db_name)
            raise_tool_error(f"db_error: {e}")

        ms = int((time.perf_counter() - t0) * 1000)
        self._audit(connection, cmd, "ok", ms=ms, db_name=db_name)
        return {"plan": plan}

    # --- helpers shared by the inspection tools -----------------------------

    def _sample_size(self, connection: str) -> int:
        cfg = self.conn_mgr.get_config(connection)
        return cfg.mongo.sample_size if cfg.mongo else 100

    def _sample_docs(
        self, connection: str, table: str, size: int, database: str | None,
    ) -> list[dict]:
        """`$sample` through `query`, keeping each document whole.

        Wrapping every document in `{doc: $$ROOT}` stops the row flattening
        from filling absent fields with None, which would make a field that
        is missing look present-but-null.
        """
        out = self.query(
            connection,
            {"aggregate": table, "pipeline": [
                {"$sample": {"size": size}},
                {"$replaceWith": {"doc": "$$ROOT"}},
            ]},
            max_rows=size,
            database=database,
        )
        return [row[0] for row in out["rows"] if row and isinstance(row[0], dict)]

    def _aggregate_docs(
        self, connection: str, table: str, pipeline: list[dict], database: str | None,
        *, max_rows: int | None = None, allow_disk_use: bool = False,
    ) -> list[dict]:
        """Run an aggregate through `query` and hand back plain documents."""
        cmd: dict[str, Any] = {"aggregate": table, "pipeline": pipeline}
        if allow_disk_use:
            cmd["allowDiskUse"] = True
        out = self.query(connection, cmd, max_rows=max_rows, database=database)
        cols = out["columns"]
        return [
            {c: v for c, v in zip(cols, row, strict=False) if v is not None}
            for row in out["rows"]
        ]
