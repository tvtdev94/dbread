"""MCP tool handlers wiring guard + rate limit + engine + audit."""

from __future__ import annotations

import time
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

from .audit import AuditLogger
from .config import Settings
from .connections import ConnectionManager
from .rate_limiter import RateLimiter
from .sql_guard import SqlGuard


class ToolError(Exception):
    """Error raised by a tool handler; surfaced to the MCP caller as JSON."""


class ToolHandlers:
    def __init__(
        self,
        settings: Settings,
        conn_mgr: ConnectionManager,
        guard: SqlGuard,
        rate_limiter: RateLimiter,
        audit: AuditLogger,
    ) -> None:
        self.settings = settings
        self.cm = conn_mgr
        self.guard = guard
        self.rl = rate_limiter
        self.audit = audit

    def list_connections(self) -> list[dict[str, str]]:
        return [{"name": n, "dialect": d} for n, d in self.cm.list_connections()]

    def list_tables(
        self, connection: str, schema: str | None = None
    ) -> list[str]:
        engine = self.cm.get_engine(connection)
        insp = sa_inspect(engine)
        return insp.get_table_names(schema=schema)

    def describe_table(
        self, connection: str, table: str, schema: str | None = None
    ) -> dict[str, Any]:
        engine = self.cm.get_engine(connection)
        insp = sa_inspect(engine)
        columns = insp.get_columns(table, schema=schema)
        indexes = insp.get_indexes(table, schema=schema)
        pks = insp.get_pk_constraint(table, schema=schema).get("constrained_columns", [])
        return {
            "columns": [
                {
                    "name": c["name"],
                    "type": str(c["type"]),
                    "nullable": c.get("nullable", True),
                    "pk": c["name"] in pks,
                }
                for c in columns
            ],
            "indexes": [
                {
                    "name": i.get("name"),
                    "columns": i.get("column_names", []),
                    "unique": i.get("unique", False),
                }
                for i in indexes
            ],
        }

    def query(
        self, connection: str, sql: str, max_rows: int | None = None
    ) -> dict[str, Any]:
        cfg = self.cm.get_config(connection)

        result = self.guard.validate(sql, cfg.dialect)
        if not result.allowed:
            self.audit.log(connection, sql, "rejected", reason=result.reason, dialect=cfg.dialect)
            raise ToolError(f"sql_guard: {result.reason}")

        effective = (
            max_rows
            if max_rows is not None and 0 < max_rows <= cfg.max_rows
            else cfg.max_rows
        )
        sql_to_run = self.guard.inject_limit(sql, cfg.dialect, effective)

        if not self.rl.acquire(connection):
            self.audit.log(connection, sql, "rejected", reason="rate_limit", dialect=cfg.dialect)
            raise ToolError("rate_limit_exceeded")

        engine = self.cm.get_engine(connection)
        t0 = time.perf_counter()
        try:
            with engine.connect() as conn:
                result_set = conn.execute(text(sql_to_run))
                columns = list(result_set.keys())
                rows = [list(r) for r in result_set.fetchmany(effective)]
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            self.audit.log(
                connection, sql_to_run, "failed", ms=ms, reason=str(e)[:200], dialect=cfg.dialect
            )
            raise ToolError(f"db_error: {e}") from e

        ms = int((time.perf_counter() - t0) * 1000)
        self.audit.log(connection, sql_to_run, "ok", rows=len(rows), ms=ms, dialect=cfg.dialect)
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": len(rows) == effective,
        }

    def explain(self, connection: str, sql: str) -> dict[str, Any]:
        cfg = self.cm.get_config(connection)

        result = self.guard.validate(sql, cfg.dialect)
        if not result.allowed:
            self.audit.log(connection, sql, "rejected", reason=result.reason, dialect=cfg.dialect)
            raise ToolError(f"sql_guard: {result.reason}")

        explain_sql = _build_explain(sql, cfg.dialect)

        if not self.rl.acquire(connection):
            self.audit.log(connection, sql, "rejected", reason="rate_limit", dialect=cfg.dialect)
            raise ToolError("rate_limit_exceeded")

        engine = self.cm.get_engine(connection)
        t0 = time.perf_counter()
        try:
            with engine.connect() as conn:
                plan = [list(r) for r in conn.execute(text(explain_sql))]
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            self.audit.log(
                connection, explain_sql, "failed", ms=ms, reason=str(e)[:200], dialect=cfg.dialect
            )
            raise ToolError(f"db_error: {e}") from e

        ms = int((time.perf_counter() - t0) * 1000)
        self.audit.log(connection, explain_sql, "ok", rows=len(plan), ms=ms, dialect=cfg.dialect)
        return {"plan": plan}


def _build_explain(sql: str, dialect: str) -> str:
    if dialect == "sqlite":
        return f"EXPLAIN QUERY PLAN {sql}"
    if dialect == "oracle":
        return f"EXPLAIN PLAN FOR {sql}"
    # postgres, mysql, mssql, and fallback all accept plain EXPLAIN
    return f"EXPLAIN {sql}"
