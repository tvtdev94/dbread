"""Tests for ConnectionManager engine lifecycle and dialect args."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from dbread.config import Settings
from dbread.connections import DIALECT_CONNECT_ARGS, ConnectionManager


def test_sqlite_engine_works(sqlite_config_yaml: Path) -> None:
    settings = Settings.load(sqlite_config_yaml)
    mgr = ConnectionManager(settings)
    engine = mgr.get_engine("mem")
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1 AS x")).scalar()
        assert result == 1
    mgr.close_all()


def test_engine_cached(sqlite_config_yaml: Path) -> None:
    settings = Settings.load(sqlite_config_yaml)
    mgr = ConnectionManager(settings)
    e1 = mgr.get_engine("mem")
    e2 = mgr.get_engine("mem")
    assert e1 is e2
    mgr.close_all()


def test_list_connections(sqlite_config_yaml: Path) -> None:
    settings = Settings.load(sqlite_config_yaml)
    mgr = ConnectionManager(settings)
    assert mgr.list_connections() == [("mem", "sqlite")]


def test_unknown_connection_raises(sqlite_config_yaml: Path) -> None:
    settings = Settings.load(sqlite_config_yaml)
    mgr = ConnectionManager(settings)
    with pytest.raises(KeyError):
        mgr.get_engine("nonexistent")


def test_postgres_connect_args_include_timeout() -> None:
    args = DIALECT_CONNECT_ARGS["postgres"](30)
    assert "statement_timeout=30000" in args["options"]
    assert "default_transaction_read_only=on" in args["options"]


def test_mysql_connect_args_include_timeout() -> None:
    args = DIALECT_CONNECT_ARGS["mysql"](15)
    assert "MAX_EXECUTION_TIME=15000" in args["init_command"]


def test_mssql_connect_args_timeout() -> None:
    args = DIALECT_CONNECT_ARGS["mssql"](20)
    assert args["timeout"] == 20


def test_get_config_returns_connection_config(sqlite_config_yaml: Path) -> None:
    settings = Settings.load(sqlite_config_yaml)
    mgr = ConnectionManager(settings)
    cfg = mgr.get_config("mem")
    assert cfg.dialect == "sqlite"
    assert cfg.max_rows == 1000


def test_tls_warn_fires_when_missing(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    import contextlib

    from dbread.config import ConnectionConfig, Settings
    from dbread.connections import ConnectionManager

    s = Settings(
        connections={
            "p": ConnectionConfig(url="postgresql+psycopg2://u:p@h:5432/db", dialect="postgres"),
        },
    )
    mgr = ConnectionManager(s)
    with caplog.at_level("WARNING", logger="dbread.connections"), contextlib.suppress(Exception):
        mgr.get_engine("p")  # no driver installed; still triggers TLS check first
    assert any("no TLS hint" in r.message for r in caplog.records)


def test_tls_warn_silent_with_sslmode(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    import contextlib

    from dbread.config import ConnectionConfig, Settings
    from dbread.connections import ConnectionManager

    s = Settings(
        connections={
            "p": ConnectionConfig(
                url="postgresql+psycopg2://u:p@h:5432/db?sslmode=require",
                dialect="postgres",
            ),
        },
    )
    mgr = ConnectionManager(s)
    with caplog.at_level("WARNING", logger="dbread.connections"), contextlib.suppress(Exception):
        mgr.get_engine("p")
    assert not any("no TLS hint" in r.message for r in caplog.records)


def test_tls_warn_skipped_for_sqlite(sqlite_config_yaml: Path, caplog) -> None:
    settings = Settings.load(sqlite_config_yaml)
    mgr = ConnectionManager(settings)
    with caplog.at_level("WARNING", logger="dbread.connections"):
        mgr.get_engine("mem")
    assert not any("no TLS hint" in r.message for r in caplog.records)
    mgr.close_all()
