"""MCP tool handlers wiring guard + rate limit + engine + audit."""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import text

from . import explore
from .audit import AuditLogger
from .config import Settings
from .connections import ConnectionManager
from .cost_guard import CostGuard
from .rate_limiter import RateLimiter
from .sql_guard import SqlGuard

if TYPE_CHECKING:
    from .mongo.tools import MongoToolHandlers


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
        self, connection: str, schema: str | None = None
    ) -> list[dict[str, str]]:
        """Every queryable relation, tagged by kind.

        Views and materialized views are listed alongside base tables: an
        agent that cannot see them concludes they do not exist and rebuilds
        the analysis from base tables instead.
        """
        cfg = self.cm.get_config(connection)
        if cfg.dialect == "mongodb":
            self._require_mongo()
            return [
                {"name": name, "type": "collection"}
                for name in self.mongo.list_tables(connection)
            ]
        engine = self.cm.get_engine(connection)
        insp = sa_inspect(engine)
        found = [{"name": n, "type": "table"} for n in insp.get_table_names(schema=schema)]
        found += [{"name": n, "type": "view"} for n in insp.get_view_names(schema=schema)]
        # Only a few dialects implement materialized views; the rest raise.
        with contextlib.suppress(NotImplementedError, AttributeError):
            found += [
                {"name": n, "type": "materialized_view"}
                for n in insp.get_materialized_view_names(schema=schema)
            ]
        return sorted(found, key=lambda r: r["name"])

    def list_schemas(self, connection: str) -> list[str]:
        """Schema names, so a multi-schema database is discoverable."""
        cfg = self.cm.get_config(connection)
        if cfg.dialect == "mongodb":
            self._require_mongo()
            # A connection is pinned to one database; listing the others
            # would hand back names outside the configured scope.
            return self.mongo.list_schemas(connection)
        engine = self.cm.get_engine(connection)
        return sa_inspect(engine).get_schema_names()

    def describe_table(
        self, connection: str, table: str, schema: str | None = None
    ) -> dict[str, Any]:
        cfg = self.cm.get_config(connection)
        if cfg.dialect == "mongodb":
            self._require_mongo()
            return self.mongo.describe_table(connection, table)
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
                    "default": _as_text(c.get("default")),
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
            # The join map. Without it an agent guesses at relationships or
            # has to go read pg_constraint by hand.
            "foreign_keys": [
                {
                    "name": fk.get("name"),
                    "columns": fk.get("constrained_columns", []),
                    "references": {
                        "schema": fk.get("referred_schema"),
                        "table": fk.get("referred_table"),
                        "columns": fk.get("referred_columns", []),
                    },
                }
                for fk in insp.get_foreign_keys(table, schema=schema)
            ],
        }

    def query(
        self,
        connection: str,
        sql: str | None = None,
        command: dict | None = None,
        max_rows: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cfg = self.cm.get_config(connection)
        if cfg.dialect == "mongodb":
            self._require_mongo()
            if sql is not None:
                raise ToolError("invalid_input: command required for mongodb connection")
            if command is None:
                raise ToolError("invalid_input: command field required")
            if params is not None:
                raise ToolError("invalid_input: params is for SQL connections only")
            return self.mongo.query(connection, command, max_rows)

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
        sql_to_run = (
            sql if params is not None
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
    ) -> dict[str, Any]:
        """Most recent rows, ordered by the best recency column available."""
        cfg = self.cm.get_config(connection)
        if cfg.dialect == "mongodb":
            self._require_mongo()
            out = self.mongo.query(
                connection, {"find": table, "sort": {"_id": -1}, "limit": n}, n
            )
            out["ordered_by"] = "_id"
            return out

        engine = self.cm.get_engine(connection)
        target = self._reflect(engine, table, schema)
        order_by = explore.recency_column(target)
        out = self.query(
            connection, sql=explore.sample_sql(engine, target, n, order_by), max_rows=n
        )
        out["ordered_by"] = order_by
        return out

    def profile_table(
        self,
        connection: str,
        table: str,
        columns: list[str] | None = None,
        schema: str | None = None,
        sample_size: int = 5000,
    ) -> dict[str, Any]:
        """Null rate, distinct count and range per column, over a sample."""
        cfg = self.cm.get_config(connection)
        if cfg.dialect == "mongodb":
            self._require_mongo()
            return self.mongo.profile_table(connection, table, columns, sample_size)

        engine = self.cm.get_engine(connection)
        target = self._reflect(engine, table, schema)
        try:
            sql, plans, total_label = explore.profile_sql(
                engine, target, columns, sample_size
            )
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
            "sample_size": sample_size,
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
    ) -> dict[str, Any]:
        cfg = self.cm.get_config(connection)
        if cfg.dialect == "mongodb":
            self._require_mongo()
            if sql is not None:
                raise ToolError("invalid_input: command required for mongodb connection")
            if command is None:
                raise ToolError("invalid_input: command field required")
            return self.mongo.explain(connection, command)

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


def _as_text(value: Any) -> str | None:
    """Render a column default for JSON; drivers hand back mixed types."""
    return None if value is None else str(value)


def _build_explain(sql: str, dialect: str) -> str:
    if dialect == "sqlite":
        return f"EXPLAIN QUERY PLAN {sql}"
    if dialect == "oracle":
        return f"EXPLAIN PLAN FOR {sql}"
    # postgres, mysql, mssql, and fallback all accept plain EXPLAIN
    return f"EXPLAIN {sql}"
