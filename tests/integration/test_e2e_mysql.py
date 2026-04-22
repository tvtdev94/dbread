"""E2E tests against a real MySQL container. Skipped if Docker absent."""

from __future__ import annotations

from pathlib import Path

import pytest

from dbread.tools import ToolError

from .conftest import build_handlers

pymysql = pytest.importorskip("pymysql")


def test_mysql_query_happy_path(mysql_url: str, tmp_path: Path) -> None:
    h = build_handlers(mysql_url, "mysql", tmp_path, max_rows=50)
    out = h.query("t", "SELECT name FROM users ORDER BY id")
    assert out["row_count"] == 3


def test_mysql_rejects_dml(mysql_url: str, tmp_path: Path) -> None:
    h = build_handlers(mysql_url, "mysql", tmp_path)
    with pytest.raises(ToolError, match="sql_guard"):
        h.query("t", "DELETE FROM users")


def test_mysql_describe(mysql_url: str, tmp_path: Path) -> None:
    h = build_handlers(mysql_url, "mysql", tmp_path)
    info = h.describe_table("t", "users")
    assert any(c["name"] == "name" for c in info["columns"])
