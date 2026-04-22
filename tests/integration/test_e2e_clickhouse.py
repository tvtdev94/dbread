"""E2E test for ClickHouse dialect (skips gracefully without Docker)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("clickhouse_sqlalchemy")

from dbread.tools import ToolError

from .conftest import build_handlers

pytestmark = pytest.mark.integration


@pytest.fixture
def clickhouse_handlers(clickhouse_url: str, tmp_path: Path):
    return build_handlers(clickhouse_url, "clickhouse", tmp_path, max_rows=50)


def test_query_happy_path(clickhouse_handlers) -> None:
    out = clickhouse_handlers.query("t", "SELECT count() AS n FROM events")
    assert out["rows"][0][0] >= 3


def test_reject_write(clickhouse_handlers) -> None:
    with pytest.raises(ToolError, match="sql_guard"):
        clickhouse_handlers.query("t", "INSERT INTO events (id, name) VALUES (99, 'x')")


def test_reject_external_table_functions(clickhouse_handlers) -> None:
    with pytest.raises(ToolError, match="function_blacklisted"):
        clickhouse_handlers.query(
            "t", "SELECT * FROM url('http://attacker/x', CSV, 'id Int32')"
        )
    with pytest.raises(ToolError, match="function_blacklisted"):
        clickhouse_handlers.query(
            "t", "SELECT * FROM remote('other-host:9000', 'db', 'table')"
        )
