"""Every catalog / statistics statement must survive the read-only guard.

These statements run through `ToolHandlers.query`, so one the guard rejects
would turn a whole `db_health` section or `list_tables` into an error on
every PostgreSQL / MySQL connection.
"""

from __future__ import annotations

import pytest

from dbread.inspection.catalog import stats_statement
from dbread.inspection.health import SECTIONS
from dbread.sql_guard import SqlGuard

HEALTH = [
    pytest.param(dialect, section.sql, id=f"{dialect}-{name}")
    for dialect, sections in SECTIONS.items()
    for name, section in sections.items()
]
STATS = [pytest.param(d, stats_statement(d, 1000), id=f"{d}-stats") for d in ("postgres", "mysql")]


@pytest.mark.parametrize(("dialect", "sql"), HEALTH + STATS)
def test_statement_passes_guard(dialect: str, sql: str) -> None:
    result = SqlGuard().validate(sql, dialect)
    assert result.allowed, result.reason


@pytest.mark.parametrize(("dialect", "sql"), HEALTH)
def test_statement_still_passes_after_row_cap_rewrite(dialect: str, sql: str) -> None:
    """A connection with max_rows below the section size gets a rewritten LIMIT."""
    guard = SqlGuard()
    rewritten = guard.inject_limit(sql, dialect, 5)
    assert "LIMIT 5" in rewritten
    assert guard.validate(rewritten, dialect).allowed


def test_stats_statement_absent_for_other_dialects() -> None:
    assert stats_statement("sqlite", 10) is None
