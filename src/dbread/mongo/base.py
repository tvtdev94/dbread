"""Shared state for MongoToolHandlers: database scoping, metadata reads, audit.

Every tool resolves its target database through `db()`, which enforces the
connection's allowlist (`mongo.databases`). Reads that bypass the query path
(listCollections, listIndexes, listDatabases) go through `_metadata()` so each
tool call still costs one rate-limit token and leaves one audit row.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from .client import MongoClientManager
from .cost_guard import MongoCostGuard
from .execute import raise_tool_error
from .guard import MongoGuard

if TYPE_CHECKING:
    from pymongo.database import Database

    from ..audit import AuditLogger
    from ..connections import ConnectionManager
    from ..rate_limiter import RateLimiter

T = TypeVar("T")

# Server-internal databases; `databases: ["*"]` never exposes them.
SYSTEM_DATABASES = frozenset({"admin", "local", "config"})


class MongoBase:
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

    # --- database scoping --------------------------------------------------

    def _configured_databases(self, connection: str) -> list[str]:
        cfg = self.conn_mgr.get_config(connection)
        return list(cfg.mongo.databases or []) if cfg.mongo else []

    def _max_time_ms(self, connection: str) -> int:
        """The connection's statement timeout, as MongoDB's `maxTimeMS`."""
        return max(1, self.conn_mgr.get_config(connection).statement_timeout_s) * 1000

    def allowed_databases(self, connection: str) -> list[str]:
        """URI database plus `mongo.databases` (`*` = every non-system db)."""
        names = {self.mc.get_db(connection).name}
        configured = self._configured_databases(connection)
        if "*" in configured:
            listed = self.mc.get_client(connection).list_databases(
                nameOnly=True, maxTimeMS=self._max_time_ms(connection),
            )
            names.update(d["name"] for d in listed if d["name"] not in SYSTEM_DATABASES)
        else:
            names.update(configured)
        return sorted(names)

    def db(self, connection: str, database: str | None = None) -> Database:
        """The database a tool call targets, refusing names outside the allowlist."""
        default = self.mc.get_db(connection)
        if database is None or database == default.name:
            return default
        if not isinstance(database, str) or not database:
            raise_tool_error("invalid_input: database must be a non-empty string")
        configured = self._configured_databases(connection)
        allowed = database in configured
        if not allowed and "*" in configured and database not in SYSTEM_DATABASES:
            try:
                allowed = database in self.allowed_databases(connection)
            except Exception as e:  # pymongo raises many types; report one shape
                raise_tool_error(f"db_error: {e}")
        if not allowed:
            raise_tool_error(f"database_not_allowed: {database}")
        return self.mc.get_client(connection)[database]

    def _resolve(self, connection: str, database: str | None, cmd: dict) -> Database:
        """`db()` that leaves a `rejected` audit row when the scope check fails."""
        from ..tools import ToolError

        try:
            return self.db(connection, database)
        except ToolError as e:
            self._audit(connection, cmd, "rejected", reason=str(e), db_name=database)
            raise

    # --- metadata reads ----------------------------------------------------

    def _metadata(
        self, connection: str, op: dict, fn: Callable[[], T], *, db_name: str | None = None,
    ) -> T:
        """Run a non-query read under one rate-limit token and one audit row."""
        self._acquire(connection, op, db_name=db_name)
        t0 = time.perf_counter()
        try:
            out = fn()
        except Exception as e:
            ms = int((time.perf_counter() - t0) * 1000)
            self._audit(connection, op, "failed", ms=ms, reason=str(e)[:200], db_name=db_name)
            raise_tool_error(f"db_error: {e}")
        ms = int((time.perf_counter() - t0) * 1000)
        rows = len(out) if isinstance(out, list | dict) else 0
        self._audit(connection, op, "ok", rows=rows, ms=ms, db_name=db_name)
        return out

    def _acquire(
        self, connection: str, cmd: dict, *,
        cost_check_ms: int | None = None, db_name: str | None = None,
    ) -> None:
        granted, scope = self.rl.acquire_with_reason(connection)
        if granted:
            return
        reason = f"rate_limit_{scope}" if scope else "rate_limit"
        self._audit(
            connection, cmd, "rejected", reason=reason,
            cost_check_ms=cost_check_ms, db_name=db_name,
        )
        raise_tool_error(f"rate_limit_exceeded: {scope}" if scope else "rate_limit_exceeded")

    @staticmethod
    def _explicit(database: str | None, db: Any) -> str | None:
        """Database name worth recording: only when the caller chose one."""
        return db.name if database is not None else None

    # --- audit ---------------------------------------------------------------

    def _audit(
        self, connection: str, cmd: dict, status: str, *,
        rows: int = 0, ms: int = 0, reason: str | None = None,
        cost_check_ms: int | None = None, db_name: str | None = None,
    ) -> None:
        to_log = cmd
        if getattr(self.audit, "redact_literals", False):
            from ..audit import redact_mongo_command
            to_log = redact_mongo_command(cmd)
        if db_name is not None:
            # Appended after redaction so the database name stays readable;
            # the audit record has no column of its own for it.
            to_log = {**to_log, "$db": db_name}
        self.audit.log(
            connection,
            json.dumps(to_log, default=str, ensure_ascii=False),
            status, rows=rows, ms=ms, reason=reason, dialect="mongodb",
            cost_check_ms=cost_check_ms,
        )
