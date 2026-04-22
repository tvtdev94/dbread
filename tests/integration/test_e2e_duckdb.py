"""E2E test for DuckDB dialect (file-based, no Docker).

Seeds a DuckDB file, opens it read-only via dbread, validates the happy path,
then checks that Layer-1 guard still blocks writes and external file readers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

duckdb = pytest.importorskip("duckdb")
pytest.importorskip("duckdb_engine")

from dbread.tools import ToolError  # noqa: E402

from .conftest import build_handlers  # noqa: E402


def _seed(db_path: Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR)")
        con.execute("INSERT INTO users VALUES (1, 'alice'), (2, 'bob'), (3, 'carol')")
    finally:
        con.close()


@pytest.fixture
def duckdb_handlers(tmp_path: Path):
    db = tmp_path / "e2e.duckdb"
    _seed(db)
    url = f"duckdb:///{db.as_posix()}?access_mode=read_only"
    return build_handlers(url, "duckdb", tmp_path, max_rows=50)


def test_query_happy_path(duckdb_handlers) -> None:
    out = duckdb_handlers.query("t", "SELECT COUNT(*) AS n FROM users")
    assert out["rows"][0] == [3]


def test_list_tables(duckdb_handlers) -> None:
    tables = duckdb_handlers.list_tables("t")
    assert "users" in tables


def test_reject_write(duckdb_handlers) -> None:
    with pytest.raises(ToolError, match="sql_guard"):
        duckdb_handlers.query("t", "INSERT INTO users VALUES (99, 'x')")
    with pytest.raises(ToolError, match="sql_guard"):
        duckdb_handlers.query("t", "CREATE TABLE junk (id INT)")


def test_reject_external_readers(duckdb_handlers) -> None:
    """read_csv / read_parquet are blacklisted (Layer-1 doc'd trade-off).

    sqlglot parses these into typed AST nodes (ReadCSV / ReadParquet), so the
    guard blocks them by class name.
    """
    with pytest.raises(ToolError, match="readcsv"):
        duckdb_handlers.query("t", "SELECT * FROM read_csv('/etc/passwd')")
    with pytest.raises(ToolError, match="readparquet"):
        duckdb_handlers.query("t", "SELECT * FROM read_parquet('x.parquet')")
