"""SQLAlchemy engine manager with per-dialect read-only safety flags."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine

from .config import Dialect, Settings

log = logging.getLogger("dbread.connections")

# Dialect -> URL keyword that indicates TLS is configured. If none of the
# keywords appear, we warn once per connection that creds are plaintext.
_TLS_HINTS: dict[str, tuple[str, ...]] = {
    "postgres": ("sslmode=",),
    "mysql": ("ssl=", "ssl_ca=", "ssl_cert=", "ssl_key="),
    "mssql": ("encrypt=",),
}


def _pg_args(timeout_s: int) -> dict[str, Any]:
    return {
        "options": (
            f"-c statement_timeout={timeout_s * 1000} "
            "-c default_transaction_read_only=on"
        ),
    }


def _mysql_args(timeout_s: int) -> dict[str, Any]:
    return {"init_command": f"SET SESSION MAX_EXECUTION_TIME={timeout_s * 1000}"}


def _mssql_args(timeout_s: int) -> dict[str, Any]:
    # pyodbc `timeout=` kwarg is LOGIN timeout only (SQL_ATTR_LOGIN_TIMEOUT).
    # Per-query timeout is wired separately via an on-connect event — see
    # `_install_mssql_query_timeout` below.
    return {"timeout": timeout_s}


def _apply_pyodbc_query_timeout(dbapi_connection: Any, timeout_s: int) -> None:
    """Assign `cnxn.timeout = N` on a pyodbc Connection (no-op on other drivers).

    pyodbc's Connection.timeout attribute bounds every cursor created by that
    connection. The `timeout=` kwarg passed to `pyodbc.connect()` only affects
    login — it does NOT bound query runtime. Hence this post-connect step.
    """
    # Non-pyodbc DBAPI (user may pin a different driver) may reject the
    # attribute assignment; swallow rather than crash the connection.
    with contextlib.suppress(AttributeError, TypeError):
        dbapi_connection.timeout = timeout_s


def _install_mssql_query_timeout(engine: Engine, timeout_s: int) -> None:
    """Register the on-connect listener that enforces per-query timeout."""

    @event.listens_for(engine, "connect")
    def _set_query_timeout(dbapi_connection, _connection_record) -> None:
        _apply_pyodbc_query_timeout(dbapi_connection, timeout_s)


def _sqlite_args(_timeout_s: int) -> dict[str, Any]:
    # Enforced by a progress handler instead — see _install_sqlite_query_timeout.
    return {}


def _oracle_args(_timeout_s: int) -> dict[str, Any]:
    # Enforced post-connect via `call_timeout` — see _install_oracle_query_timeout.
    return {}


def _duckdb_args(_timeout_s: int) -> dict[str, Any]:
    # read-only mode is expressed in the URL: duckdb:///path?access_mode=read_only
    # DuckDB exposes no statement-timeout knob; documented as unsupported.
    return {}


# How often SQLite runs the progress callback, in VM instructions. Small
# enough to abort promptly, large enough that the callback is not the cost.
_SQLITE_PROGRESS_OPS = 10_000
_TIMEOUT_STATE = "dbread_statement_started"


def _install_sqlite_query_timeout(engine: Engine, timeout_s: int) -> None:
    """Abort SQLite statements that outrun the configured timeout.

    SQLite has no server-side timeout; the progress handler is the supported
    interrupt point. State lives in the pool's per-connection `info` dict
    because `sqlite3.Connection` rejects attribute assignment.
    """

    @event.listens_for(engine, "connect")
    def _register(dbapi_connection, connection_record) -> None:
        state: dict[str, float | None] = {"started": None}
        connection_record.info[_TIMEOUT_STATE] = state

        def _abort_when_overdue() -> int:
            started = state["started"]
            overdue = started is not None and time.monotonic() - started > timeout_s
            return 1 if overdue else 0  # non-zero interrupts the statement

        with contextlib.suppress(AttributeError, TypeError):
            dbapi_connection.set_progress_handler(
                _abort_when_overdue, _SQLITE_PROGRESS_OPS
            )

    @event.listens_for(engine, "before_cursor_execute")
    def _start_clock(conn, _cursor, _statement, _params, _context, _executemany) -> None:
        state = conn.connection.info.get(_TIMEOUT_STATE)
        if state is not None:
            state["started"] = time.monotonic()


def _install_oracle_query_timeout(engine: Engine, timeout_s: int) -> None:
    """Bound each round trip via python-oracledb's `call_timeout` (ms)."""

    @event.listens_for(engine, "connect")
    def _set_call_timeout(dbapi_connection, _connection_record) -> None:
        with contextlib.suppress(AttributeError, TypeError):
            dbapi_connection.call_timeout = timeout_s * 1000


def _clickhouse_args(timeout_s: int) -> dict[str, Any]:
    # Layer-0 belt-and-braces: force readonly=1 at connect time even if the
    # DB user's profile wasn't set up; plus bound each query's wall time.
    return {"settings": {"readonly": 1, "max_execution_time": timeout_s}}


# Dialects whose timeout cannot ride along in connect_args and needs an
# on-connect listener instead. DuckDB is absent: it has no such knob.
_POST_CONNECT_TIMEOUT: dict[str, Callable[[Engine, int], None]] = {
    "mssql": _install_mssql_query_timeout,
    "sqlite": _install_sqlite_query_timeout,
    "oracle": _install_oracle_query_timeout,
}

DIALECT_CONNECT_ARGS: dict[Dialect, Callable[[int], dict[str, Any]]] = {
    "postgres": _pg_args,
    "mysql": _mysql_args,
    "mssql": _mssql_args,
    "sqlite": _sqlite_args,
    "oracle": _oracle_args,
    "duckdb": _duckdb_args,
    "clickhouse": _clickhouse_args,
}


def _warn_tls(name: str, url: str, dialect: str) -> None:
    hints = _TLS_HINTS.get(dialect)
    if not hints:
        return
    lower = url.lower()
    if not any(h in lower for h in hints):
        log.warning(
            "connection %r (%s) has no TLS hint (%s) in URL; credentials may travel plaintext",
            name, dialect, "|".join(hints),
        )


class ConnectionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._engines: dict[str, Engine] = {}

    def get_engine(self, name: str) -> Engine:
        cached = self._engines.get(name)
        if cached is not None:
            return cached
        cfg = self.settings.connections.get(name)
        if cfg is None:
            raise KeyError(f"unknown connection: {name!r}")
        url = cfg.resolved_url()
        _warn_tls(name, url, cfg.dialect)
        connect_args = DIALECT_CONNECT_ARGS[cfg.dialect](cfg.statement_timeout_s)
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args=connect_args,
            echo=False,
        )
        installer = _POST_CONNECT_TIMEOUT.get(cfg.dialect)
        if installer is not None:
            installer(engine, cfg.statement_timeout_s)
        self._engines[name] = engine
        return engine

    def list_connections(self) -> list[tuple[str, str]]:
        return [(name, cfg.dialect) for name, cfg in self.settings.connections.items()]

    def get_config(self, name: str):
        cfg = self.settings.connections.get(name)
        if cfg is None:
            raise KeyError(f"unknown connection: {name!r}")
        return cfg

    def close_all(self) -> None:
        for engine in self._engines.values():
            engine.dispose()
        self._engines.clear()
