"""Pre-execution cost guard — estimate rows via EXPLAIN before run.

Fail-open semantics: unsupported dialects, DB errors, missing plan keys all
return ``None`` so the caller proceeds (Layers 2/3/4 still cover). Caller
decides whether the estimate exceeds threshold.

Parser registry pattern keeps per-dialect logic isolated. v0.8.0 ships PG +
MySQL; phase 02 adds MSSQL/Oracle/DuckDB/Mongo.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import secrets
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger("dbread.cost_guard")


class CostGuard:
    """Estimate result/scanned rows via dialect-specific EXPLAIN parsing."""

    def check(
        self,
        sql: str,
        dialect: str,
        engine: Engine,
        threshold: int | None,
    ) -> tuple[int | None, int]:
        """Return ``(rows_estimate, cost_check_ms)``.

        ``rows_estimate=None`` means unsupported dialect / planner failure /
        unparseable plan — caller proceeds (fail-open). When ``threshold`` is
        ``None`` the check is disabled and returns ``(None, 0)`` immediately.
        """
        if threshold is None:
            return None, 0
        parser = _PARSERS.get(dialect)
        if parser is None:
            return None, 0
        t0 = time.perf_counter()
        try:
            rows = parser(engine, sql)
        except Exception as e:
            log.debug("cost_guard parser failed for %s: %s", dialect, e)
            rows = None
        ms = int((time.perf_counter() - t0) * 1000)
        return rows, ms


def _parse_pg(engine: Engine, sql: str) -> int | None:
    """Parse Postgres ``EXPLAIN (FORMAT JSON)`` → top-level Plan Rows."""
    explain_sql = f"EXPLAIN (FORMAT JSON) {sql}"
    with engine.connect() as conn:
        row = conn.execute(text(explain_sql)).fetchone()
    if row is None:
        return None
    raw = row[0]
    # psycopg2 returns str; psycopg3 / asyncpg may return list/dict already
    plan = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(plan, list) or not plan:
        return None
    top = plan[0].get("Plan") if isinstance(plan[0], dict) else None
    if not isinstance(top, dict):
        return None
    rows = top.get("Plan Rows")
    if rows is None:
        return None
    try:
        return int(rows)
    except (TypeError, ValueError):
        return None


def _parse_mysql(engine: Engine, sql: str) -> int | None:
    """Parse MySQL ``EXPLAIN FORMAT=JSON`` → sum rows_examined_per_scan."""
    explain_sql = f"EXPLAIN FORMAT=JSON {sql}"
    with engine.connect() as conn:
        row = conn.execute(text(explain_sql)).fetchone()
    if row is None:
        return None
    raw = row[0]
    plan = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(plan, dict):
        return None
    block = plan.get("query_block")
    if not isinstance(block, dict):
        return None
    total = _mysql_sum_rows(block)
    # 0 ⇒ planner had no stats; treat as "unknown" (fail-open) rather than 0
    return total if total > 0 else None


def _mysql_sum_rows(node: Any) -> int:
    """Walk MySQL plan tree summing every ``rows_examined_per_scan`` seen.

    Skips recursion into nested ``query_block`` keys so subqueries
    (``materialized_from_subquery``, ``attached_subqueries``) are not
    double-counted on top of their parent's already-materialized
    ``rows_examined_per_scan``.
    """
    total = 0
    if isinstance(node, dict):
        v = node.get("rows_examined_per_scan")
        if v is not None:
            with contextlib.suppress(TypeError, ValueError):
                total += int(v)
        for k, child in node.items():
            if k == "query_block":
                continue
            if isinstance(child, (dict, list)):
                total += _mysql_sum_rows(child)
    elif isinstance(node, list):
        for item in node:
            total += _mysql_sum_rows(item)
    return total


def _parse_mssql(engine: Engine, sql: str) -> int | None:
    """Parse MSSQL ``SET SHOWPLAN_XML ON`` → root StmtSimple/@StatementEstRows.

    SHOWPLAN_XML is plan-only (does NOT execute the statement). Requires the
    ``SHOWPLAN`` permission; missing it raises and we fail-open. If
    ``SET SHOWPLAN_XML OFF`` fails the session is left in plan-emit mode —
    invalidate the connection so the pool drops it instead of returning XML
    on the next checkout.
    """
    with engine.connect() as conn:
        showplan_on = False
        try:
            conn.execute(text("SET SHOWPLAN_XML ON"))
            showplan_on = True
            row = conn.execute(text(sql)).fetchone()
        finally:
            if showplan_on:
                try:
                    conn.execute(text("SET SHOWPLAN_XML OFF"))
                except Exception:
                    conn.invalidate()  # session unrecoverable — drop from pool
    if row is None or row[0] is None:
        return None
    try:
        root = ET.fromstring(row[0])
    except ET.ParseError:
        return None
    # XML namespace varies by SQL Server version — search by local-name.
    for elem in root.iter():
        local = elem.tag.rsplit("}", 1)[-1]
        if local == "StmtSimple":
            est = elem.get("StatementEstRows")
            if est:
                with contextlib.suppress(TypeError, ValueError):
                    return int(float(est))
    return None


def _parse_oracle(engine: Engine, sql: str) -> int | None:
    """Parse Oracle ``EXPLAIN PLAN FOR …`` → ``PLAN_TABLE.cardinality`` at id=0.

    Requires INSERT on PLAN_TABLE; missing perm raises and we fail-open.
    Uses a per-call ``STATEMENT_ID`` (16 hex chars from ``secrets``) so
    concurrent dbread instances sharing PLAN_TABLE never read each other's
    rows. Always DELETE the rows on exit — fail-open if delete denied.
    """
    stmt_id = f"dbread_{secrets.token_hex(8)}"
    with engine.connect() as conn:
        try:
            conn.execute(text(f"EXPLAIN PLAN SET STATEMENT_ID = '{stmt_id}' FOR {sql}"))
            row = conn.execute(
                text(
                    "SELECT cardinality FROM PLAN_TABLE "
                    "WHERE id = 0 AND statement_id = :sid"
                ),
                {"sid": stmt_id},
            ).fetchone()
        finally:
            with contextlib.suppress(Exception):
                conn.execute(
                    text("DELETE FROM PLAN_TABLE WHERE statement_id = :sid"),
                    {"sid": stmt_id},
                )
    if row is None or row[0] is None:
        return None
    with contextlib.suppress(TypeError, ValueError):
        return int(row[0])
    return None


def _parse_duckdb(engine: Engine, sql: str) -> int | None:
    """Parse DuckDB ``EXPLAIN <sql>`` text output for ``EC: <N>`` cardinality.

    DuckDB shows EC at every plan node and may echo user SQL literals (e.g.
    ``SELECT 'EC: 1' …``) — to defeat injection bypass we take ``max()``
    across all matches. A user-injected ``'EC: 0'`` cannot reduce the real
    plan estimate; injecting a huge value causes self-DOS rather than
    bypass, which is acceptable.
    """
    with engine.connect() as conn:
        rows = conn.execute(text(f"EXPLAIN {sql}")).fetchall()
    text_plan = "\n".join(str(r) for r in rows)
    # Variants across versions: "EC: 1234", "Estimated Cardinality: 1234".
    matches = re.findall(
        r"(?:\bEC\b|Estimated\s+Cardinality|estimated\s+rows)\s*[:=]?\s*(\d+)",
        text_plan, re.IGNORECASE,
    )
    if not matches:
        return None
    with contextlib.suppress(TypeError, ValueError):
        return max(int(m) for m in matches)
    return None


_PARSERS: dict[str, Callable[[Engine, str], int | None]] = {
    "postgres": _parse_pg,
    "mysql": _parse_mysql,
    "mssql": _parse_mssql,
    "oracle": _parse_oracle,
    "duckdb": _parse_duckdb,
}
