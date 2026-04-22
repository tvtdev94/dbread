"""Tests for config loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from dbread.config import ConnectionConfig, Settings


def test_load_minimal(sqlite_config_yaml: Path) -> None:
    s = Settings.load(sqlite_config_yaml)
    assert "mem" in s.connections
    assert s.connections["mem"].dialect == "sqlite"
    assert s.connections["mem"].max_rows == 1000


def test_url_xor_url_env_both_set() -> None:
    with pytest.raises(ValidationError):
        ConnectionConfig(url="x", url_env="Y", dialect="sqlite")


def test_url_xor_url_env_neither_set() -> None:
    with pytest.raises(ValidationError):
        ConnectionConfig(dialect="sqlite")


def test_url_env_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_DB_URL", "sqlite:///x.db")
    c = ConnectionConfig(url_env="MY_DB_URL", dialect="sqlite")
    assert c.resolved_url() == "sqlite:///x.db"


def test_url_env_missing_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOT_SET_URL", raising=False)
    c = ConnectionConfig(url_env="NOT_SET_URL", dialect="sqlite")
    with pytest.raises(ValueError, match="not set"):
        c.resolved_url()


def test_defaults_applied() -> None:
    c = ConnectionConfig(url="sqlite:///a.db", dialect="sqlite")
    assert c.rate_limit_per_min == 60
    assert c.statement_timeout_s == 30
    assert c.max_rows == 1000


def test_empty_connections_rejected(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text(yaml.safe_dump({"connections": {}}), encoding="utf-8")
    with pytest.raises(ValidationError):
        Settings.load(path)


def test_unknown_dialect_rejected() -> None:
    with pytest.raises(ValidationError):
        ConnectionConfig(url="x", dialect="cassandra")  # type: ignore[arg-type]
