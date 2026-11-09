"""MCP tool handlers wiring guard + rate limit + engine + audit."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

from . import explore
from .audit import AuditLogger
from .config import Settings
from .connections import ConnectionManager
from .cost_guard import CostGuard
from .inspection import catalog
from .inspection_tools import InspectionTools
from .rate_limiter import RateLimiter
from .sql_guard import SqlGuard

if TYPE_CHECKING:
    from .mongo.tools import MongoToolHandlers


class ToolError(Exception):
    """Error raised by a tool handler; surfaced to the MCP caller as JSON."""


class ToolHandlers(InspectionTools):
    def __init__(
        self,
        settings: Settings,
        conn_mgr: ConnectionManager,
        guard: SqlGuard,
        rate_limiter: RateLimiter,
        audit: AuditLogger,
        cost_guard: CostGuard | None = None,
        mongo: MongoToolHandlers | None = None,
    ) -> None:
        self.settings = settings
        self.cm = conn_mgr
        self.guard = guard
        self.rl = rate_limiter
        self.audit = audit
        self.cost_guard = cost_guard or CostGuard()
        self.mongo = mongo

    def list_connections(self) -> list[dict[str, str]]:
        return [{"name": n, "dialect": d} for n, d in self.cm.list_connections()]

    def list_tables(
        self, connection: str, schema: str | None = None, database: str | None = None
    ) -> list[dict[str, Any]]:
        """Every queryable relation, tagged by kind, with size where known.

        Views and materialized views are listed alongside base tables: an
        agent that cannot see them concludes they do not exist and rebuilds
        the analysis from base tables instead.
        """
        cfg = self._route(connection, database)
        if cfg.dialect == "mongodb":
            return self.mongo.list_tables(connection, **_db(database))
        engine = self.cm.get_engine(connection)
        insp = sa_inspect(engine)
        found = catalog.list_relations(insp, schema)
        stats_sql = catalog.stats_statement(cfg.dialect, cfg.max_rows)
        rows: list[list[Any]] = []
        if stats_sql is not None:
            resolved = schema if schema is not None else insp.default_schema_name
            rows = self.query(connection, sql=stats_sql, params={"schema": resolved})["rows"]
        catalog.attach_stats(found, rows)
        return found

    def list_schemas(self, connection: str, database: str | None = None) -> list[str]:
        """Schema names, so a multi-schema database is discoverable."""
        cfg = self._route(connection, database)
        if cfg.dialect == "mongodb":
            # Only the databases this connection is allowed to read.
            return self.mongo.list_schemas(connection, **_db(database))
        engine = self.cm.get_engine(connection)
        return sa_inspect(engine).get_schema_names()

    def describe_table(
        self,
        connection: str,
        table: str,
        schema: str | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        cfg = self._route(connection, database)
        if cfg.dialect == "mongodb":
            return self.mongo.describe_table(connection, table, **_db(database))
        return catalog.describe(sa_inspect(self.cm.get_engine(connection)), table, schema)

    def query(
        self,
        connection: str,
        sql: str | None = None,
        command: dict | None = None,
        max_rows: int | None = None,
        params: dict[str, Any] | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        cfg = self._route(connection, database)
        if cfg.dialect == "mongodb":
            if sql is not None:
                raise ToolError("invalid_input: command required for mongodb connection")
            if command is None:
                raise ToolError("invalid_input: command field required")
            if params is not None:
                raise ToolError("invalid_input: params is for SQL connections only")
            return self.mongo.query(connection, command, max_rows, **_db(database))

        if command is not None:
            raise ToolError("invalid_input: sql required for SQL connection")
        if sql is None:
            raise ToolError("invalid_input: sql field required")

        result = self.guard.validate(sql, cfg.dialect)
        if not result.allowed:
            self.audit.log(connection, sql, "rejected", reason=result.reason, dialect=cfg.dialect)
            raise ToolError(f"sql_guard: {result.reason}")

        effective = (
            max_rows
            if max_rows is not None and 0 < max_rows <= cfg.max_rows
            else cfg.max_rows
        )
        # Re-serializing a parameterized statement would rewrite `:name` into
        # whatever placeholder style sqlglot prefers for the dialect, which is
        # not always the one the driver accepts. Leave it alone and let the
        # fetch cap bound the result, the same fallback already used when
        # sqlglot cannot parse a statement at all.
        # Empty params is the same as none: a client that serializes an
        # omitted object property as {} must not lose the row bound.
        sql_to_run = (
            sql if params
            else self.guard.inject_limit(sql, cfg.dialect, effective)
        )

        engine = self.cm.get_engine(connection)
        cost_ms: int | None = None
        if cfg.max_rows_estimate is not None:
            # EXPLAIN raw `sql` (not `sql_to_run`) so the planner reports the
            # un-LIMIT'd cost — otherwise injected LIMIT would mask full scans.
            rows_est, cost_ms = self.cost_guard.check(
                sql, cfg.dialect, engine, cfg.max_rows_estimate
            )
            if rows_est is not None and rows_est > cfg.max_rows_estimate:
                reason = (
                    f"cost_guard_error: rows_estimate={rows_est} "
                    f"exceeds {cfg.max_rows_estimate}"
                )
                self.audit.log(
                    connection, sql, "rejected", reason=reason,
                    dialect=cfg.dialect, cost_check_ms=cost_ms,
                )
                raise ToolError(reason)

        granted, scope = self.rl.acquire_with_reason(connection)
        if not granted:
            reason = f"rate_limit_{scope}" if scope else "rate_limit"
            self.audit.log(
                connection, sql, "rejected", reason=reason,
                dialect=cfg.dialect, cost_check_ms=cost_ms,
            )
            raise ToolError(f"rate_limit_exceeded: {scope}" if scope else "rate_limit_exceeded")

        t0 = time.perf_counter()
        try:
            with engine.connect() as conn:
                # Without params the statement goes straight to the driver:
                # SQLAlchemy's text() reads `:name` as a bind placeholder, and
                # compact JSON literals like '{"a":1}' trip over that.
                result_set = (
                    conn.execute(text(sql_to_run), params)
                    if params is not None
                    else conn.exec_driver_sql(sql_to_run)
                )
                columns = list(result_set.keys())
                rows = [list(r) for r in result_set.fetchmany(effective)]
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            self.audit.log(
                connection, sql_to_run, "failed", ms=ms, reason=str(e)[:200],
                dialect=cfg.dialect, cost_check_ms=cost_ms,
            )
            raise ToolError(f"db_error: {e}") from e

        ms = int((time.perf_counter() - t0) * 1000)
        self.audit.log(
            connection, sql_to_run, "ok", rows=len(rows), ms=ms,
            dialect=cfg.dialect, cost_check_ms=cost_ms,
        )
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            # Conservative: landing exactly on the cap reports truncated even
            # when the result happened to end there. Over-reporting costs one
            # extra query; under-reporting would hide rows.
            "truncated": len(rows) == effective,
        }

    def sample_table(
        self,
        connection: str,
        table: str,
        n: int = 20,
        schema: str | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        """Most recent rows, ordered by the best recency column available."""
        cfg = self._route(connection, database)
        # The declared inputSchema is advertising — the MCP SDK does not
        # enforce it — so every caller-supplied bound is clamped here.
        rows = _clamp(n, 1, cfg.max_rows)
        if cfg.dialect == "mongodb":
            out = self.mongo.query(
                connection, {"find": table, "sort": {"_id": -1}, "limit": rows}, rows,
                **_db(database),
            )
            out["ordered_by"] = "_id"
            return out

        engine = self.cm.get_engine(connection)
        target = self._reflect(engine, table, schema)
        order_by = explore.recency_column(target)
        out = self.query(
            connection,
            sql=explore.sample_sql(engine, target, rows, order_by),
            max_rows=rows,
        )
        out["ordered_by"] = order_by
        return out

    def profile_table(
        self,
        connection: str,
        table: str,
        columns: list[str] | None = None,
        schema: str | None = None,
        sample_size: int | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        """Null rate, distinct count and range per column, over a sample.

        `sample_size` is left unset by default so each backend can apply its
        own: MongoDB honours the connection's `mongo.sample_size`, SQL uses
        its own default. This tool returns a single row, so `max_rows` never
        bounds it — the sample size is the only limit on how much the server
        reads, which is why it is clamped rather than trusted.
        """
        cfg = self._route(connection, database)
        if cfg.dialect == "mongodb":
            return self.mongo.profile_table(
                connection, table, columns, sample_size, **_db(database)
            )

        size = _clamp(
            sample_size if sample_size is not None else explore.DEFAULT_SAMPLE_SIZE,
            1,
            explore.MAX_SAMPLE_SIZE,
        )
        engine = self.cm.get_engine(connection)
        target = self._reflect(engine, table, schema)
        try:
            sql, plans, total_label = explore.profile_sql(engine, target, columns, size)
        except ValueError as e:
            raise ToolError(f"invalid_input: {e}") from e

        out = self.query(connection, sql=sql)
        if not out["rows"]:
            return {"table": table, "sampled_rows": 0, "fields": []}
        row = dict(zip(out["columns"], out["rows"][0], strict=False))
        sampled, fields = explore.build_profile(row, plans, total_label)
        return {
            "table": table,
            "sampled_rows": sampled,
            "sample_size": size,
            "source": "sampled",
            "fields": fields,
        }

    def _reflect(self, engine: Any, table: str, schema: str | None):
        try:
            return explore.reflect(engine, table, schema)
        except Exception as e:
            raise ToolError(f"unknown_table: {table} ({type(e).__name__})") from e

    def explain(
        self,
        connection: str,
        sql: str | None = None,
        command: dict | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        cfg = self._route(connection, database)
        if cfg.dialect == "mongodb":
            if sql is not None:
                raise ToolError("invalid_input: command required for mongodb connection")
            if command is None:
                raise ToolError("invalid_input: command field required")
            return self.mongo.explain(connection, command, **_db(database))

        if command is not None:
            raise ToolError("invalid_input: sql required for SQL connection")
        if sql is None:
            raise ToolError("invalid_input: sql field required")

        result = self.guard.validate(sql, cfg.dialect)
        if not result.allowed:
            self.audit.log(connection, sql, "rejected", reason=result.reason, dialect=cfg.dialect)
            raise ToolError(f"sql_guard: {result.reason}")

        explain_sql = _build_explain(sql, cfg.dialect)

        granted, scope = self.rl.acquire_with_reason(connection)
        if not granted:
            reason = f"rate_limit_{scope}" if scope else "rate_limit"
            self.audit.log(connection, sql, "rejected", reason=reason, dialect=cfg.dialect)
            raise ToolError(f"rate_limit_exceeded: {scope}" if scope else "rate_limit_exceeded")

        engine = self.cm.get_engine(connection)
        t0 = time.perf_counter()
        try:
            with engine.connect() as conn:
                plan = [list(r) for r in conn.exec_driver_sql(explain_sql)]
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            self.audit.log(
                connection, explain_sql, "failed", ms=ms, reason=str(e)[:200], dialect=cfg.dialect
            )
            raise ToolError(f"db_error: {e}") from e

        ms = int((time.perf_counter() - t0) * 1000)
        self.audit.log(connection, explain_sql, "ok", rows=len(plan), ms=ms, dialect=cfg.dialect)
        return {"plan": plan}

    def _require_mongo(self) -> None:
        if self.mongo is None:
            raise ToolError("mongo_not_configured")


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _db(database: str | None) -> dict[str, str]:
    """`database=` for a MongoToolHandlers call, only when the caller chose one.

    Leaving it out when unset keeps the handler's own default (the URI
    database) in charge.
    """
    return {} if database is None else {"database": database}


def _build_explain(sql: str, dialect: str) -> str:
    if dialect == "sqlite":
        return f"EXPLAIN QUERY PLAN {sql}"
    if dialect == "oracle":
        return f"EXPLAIN PLAN FOR {sql}"
    # postgres, mysql, mssql, and fallback all accept plain EXPLAIN
    return f"EXPLAIN {sql}"
