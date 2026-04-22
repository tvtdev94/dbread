"""SQLAlchemy engine manager with per-dialect read-only safety flags."""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from .config import Dialect, Settings


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
    return {"timeout": timeout_s}


def _sqlite_args(_timeout_s: int) -> dict[str, Any]:
    return {}


def _oracle_args(_timeout_s: int) -> dict[str, Any]:
    return {}


DIALECT_CONNECT_ARGS: dict[Dialect, Callable[[int], dict[str, Any]]] = {
    "postgres": _pg_args,
    "mysql": _mysql_args,
    "mssql": _mssql_args,
    "sqlite": _sqlite_args,
    "oracle": _oracle_args,
}


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
        connect_args = DIALECT_CONNECT_ARGS[cfg.dialect](cfg.statement_timeout_s)
        engine = create_engine(
            cfg.resolved_url(),
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args=connect_args,
            echo=False,
        )
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
