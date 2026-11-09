"""Whole-database inspection tools, mixed into `ToolHandlers`.

MongoDB connections delegate to `MongoToolHandlers`. SQL connections read
the catalog through the inspector (one rate-limit token and one audit row per
call) and read rows or statistics views only through `self.query`, so the
guard, cost guard, rate limit, audit and statement timeout apply to each
statement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import inspect as sa_inspect

from .inspection import catalog, integrity, snapshot
from .inspection.health import SECTION_ROWS, SECTIONS

if TYPE_CHECKING:
    from .audit import AuditLogger
    from .config import ConnectionConfig
    from .connections import ConnectionManager
    from .mongo.tools import MongoToolHandlers
    from .rate_limiter import RateLimiter


class InspectionTools:
    # Provided by ToolHandlers.
    cm: ConnectionManager
    rl: RateLimiter
    audit: AuditLogger
    mongo: MongoToolHandlers | None

    def list_databases(self, connection: str) -> list[dict[str, Any]]:
        self._mongo_only(connection, "list_databases")
        return self.mongo.list_databases(connection)

    def collection_stats(
        self, connection: str, table: str, database: str | None = None
    ) -> dict[str, Any]:
        self._mongo_only(connection, "collection_stats")
        return self.mongo.collection_stats(connection, table, database=database)

    def database_overview(
        self, connection: str, schema: str | None = None, database: str | None = None
    ) -> dict[str, Any]:
        cfg = self._route(connection, database)
        if cfg.dialect == "mongodb":
            return self.mongo.database_overview(connection, database=database)
        insp = self._metadata(connection, "database_overview", schema)
        relations = self.list_tables(connection, schema)
        resolved = schema if schema is not None else insp.default_schema_name
        return {"connection": connection, "schema": resolved,
                **catalog.overview(insp, schema, relations)}

    def search_schema(
        self,
        connection: str,
        pattern: str,
        schema: str | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(pattern, str) or not pattern.strip():
            raise _tool_error("invalid_input: pattern must be a non-empty string")
        cfg = self._route(connection, database)
        if cfg.dialect == "mongodb":
            return self.mongo.search_schema(connection, pattern, database=database)
        insp = self._metadata(connection, "search_schema", schema)
        relations = catalog.list_relations(insp, schema)
        return catalog.search(insp, schema, relations, pattern)

    def check_integrity(
        self,
        connection: str,
        table: str,
        checks: list[str] | None = None,
        references: list[dict[str, Any]] | None = None,
        unique_columns: list[str] | None = None,
        schema: str | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        # The MCP SDK does not enforce inputSchema, so the list shapes are checked here.
        _require_str_list(checks, "checks")
        _require_str_list(unique_columns, "unique_columns")
        unknown = [c for c in checks or [] if c not in integrity.CHECKS]
        if unknown:
            raise _tool_error(
                f"invalid_input: unknown check {unknown[0]!r}; "
                f"expected one of {', '.join(integrity.CHECKS)}"
            )
        cfg = self._route(connection, database)
        if cfg.dialect == "mongodb":
            return self.mongo.check_integrity(
                connection, table, checks=checks, references=references,
                unique_columns=unique_columns, database=database,
            )
        if references is not None:
            raise _tool_error(
                "invalid_input: references is for mongodb connections only; "
                "SQL checks use the declared foreign keys"
            )
        engine = self.cm.get_engine(connection)
        target = self._reflect(engine, table, schema)
        missing = [c for c in unique_columns or [] if c not in target.c]
        if missing:
            raise _tool_error(f"invalid_input: unknown column: {missing[0]}")
        results = integrity.run_checks(
            engine,
            target,
            list(checks) if checks else list(integrity.CHECKS),
            unique_columns,
            lambda sql, rows: self.query(connection, sql=sql, max_rows=rows),
        )
        return {"table": table, "checks": results}

    def diff_schema(
        self,
        connection_a: str,
        connection_b: str,
        schema_a: str | None = None,
        schema_b: str | None = None,
        database_a: str | None = None,
        database_b: str | None = None,
    ) -> dict[str, Any]:
        label_a, snap_a = self._snapshot(connection_a, schema_a, database_a)
        label_b, snap_b = self._snapshot(connection_b, schema_b, database_b)
        return snapshot.diff(label_a, label_b, snap_a, snap_b)

    def db_health(self, connection: str) -> dict[str, Any]:
        from .tools import ToolError

        cfg = self.cm.get_config(connection)
        sections = SECTIONS.get(cfg.dialect)
        if sections is None:
            raise _tool_error(f"unsupported_dialect: {cfg.dialect}")
        out: dict[str, Any] = {}
        for name, section in sections.items():
            try:
                res = self.query(connection, sql=section.sql, max_rows=SECTION_ROWS)
            except ToolError as e:
                # Only a database error is fixed by a grant or extension; a
                # rate-limit or guard rejection would make the hint misleading.
                hint = section.hint if str(e).startswith("db_error") else None
                out[name] = {"error": str(e), "hint": hint}
                continue
            out[name] = {"rows": [dict(zip(res["columns"], row, strict=False))
                                  for row in res["rows"]]}
        return {"dialect": cfg.dialect, "sections": out}

    # --- helpers -----------------------------------------------------------

    def _snapshot(
        self, connection: str, schema: str | None, database: str | None
    ) -> tuple[str, dict[str, Any]]:
        cfg = self._route(connection, database)
        if cfg.dialect == "mongodb":
            label = f"{connection}/{database}" if database else connection
            return label, self.mongo.schema_snapshot(connection, database=database)
        insp = self._metadata(connection, "diff_schema", schema)
        relations = catalog.list_relations(insp, schema)
        label = f"{connection}/{schema}" if schema else connection
        return label, snapshot.sql_snapshot(insp, schema, relations)

    def _route(self, connection: str, database: str | None) -> ConnectionConfig:
        """Config for `connection`; rejects `database` on SQL connections."""
        cfg = self.cm.get_config(connection)
        if cfg.dialect == "mongodb":
            self._require_mongo()
        elif database is not None:
            raise _tool_error("invalid_input: database is for mongodb connections only")
        return cfg

    def _mongo_only(self, connection: str, tool: str) -> None:
        if self.cm.get_config(connection).dialect != "mongodb":
            raise _tool_error(f"invalid_input: {tool} is for mongodb connections only")
        self._require_mongo()

    def _metadata(self, connection: str, tool: str, schema: str | None) -> Any:
        """Charge one rate-limit token and write one audit row for a catalog read."""
        cfg = self.cm.get_config(connection)
        label = f"-- {tool}" + (f" schema={schema}" if schema else "")
        granted, scope = self.rl.acquire_with_reason(connection)
        if not granted:
            reason = f"rate_limit_{scope}" if scope else "rate_limit"
            self.audit.log(connection, label, "rejected", reason=reason, dialect=cfg.dialect)
            raise _tool_error(
                f"rate_limit_exceeded: {scope}" if scope else "rate_limit_exceeded"
            )
        self.audit.log(connection, label, "ok", dialect=cfg.dialect)
        return sa_inspect(self.cm.get_engine(connection))


def _tool_error(message: str) -> Exception:
    # Imported late: tools.py imports this module to build ToolHandlers.
    from .tools import ToolError

    return ToolError(message)


def _require_str_list(value: Any, name: str) -> None:
    if value is not None and (
        not isinstance(value, list) or not all(isinstance(v, str) for v in value)
    ):
        raise _tool_error(f"invalid_input: {name} must be a list of strings")
