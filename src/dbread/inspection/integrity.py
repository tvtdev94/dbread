"""Exact whole-table integrity checks for SQL connections.

Every statement is SQLAlchemy Core compiled for the connection's dialect,
then handed to `run` — the guarded query path — so cost guard, statement
timeout, rate limit and audit apply to each one. A check that fails records
its error and the remaining checks still run.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import Boolean, Table, and_, case, distinct, func, select, true
from sqlalchemy.engine import Engine

from ..explore import COMPARABLE_TYPES, MAX_PROFILE_COLUMNS, ORDERABLE_TYPES, _compile

CHECKS = ("orphans", "duplicates", "null_columns", "constant_columns")
EXAMPLE_LIMIT = 5

# (sql, max_rows) -> the `query` tool's result dict.
RunQuery = Callable[[str, int], dict[str, Any]]


def default_checks(unique_columns: list[str] | None) -> list[str]:
    """Every check that can run; duplicates only when there is a key to check."""
    return [c for c in CHECKS if c != "duplicates" or unique_columns]


def run_checks(
    engine: Engine,
    table: Table,
    checks: list[str],
    unique_columns: list[str] | None,
    run: RunQuery,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    if "orphans" in checks:
        # A set on the Table; sort so results come back in a stable order.
        for fkc in sorted(table.foreign_key_constraints, key=lambda f: f.column_keys):
            pairs = [(el.parent, el.column) for el in fkc.elements]
            target = (
                f"{','.join(c.name for c, _ in pairs)} -> "
                f"{fkc.referred_table.name}({','.join(r.name for _, r in pairs)})"
            )
            results.append(_guarded("orphans", target, lambda p=pairs, f=fkc: _orphans(
                engine, table, f.referred_table, p, run)))
    if "duplicates" in checks:
        if unique_columns:
            results.append(_guarded("duplicates", ",".join(unique_columns), lambda: _duplicates(
                engine, table, unique_columns, run)))
        else:
            results.append(_entry("duplicates", "", None, [], "unique_columns required"))
    wanted = [c for c in ("null_columns", "constant_columns") if c in checks]
    if wanted:
        try:
            stats, error = _column_stats(engine, table, run), None
        except Exception as e:
            stats, error = None, _message(e)
        for check in wanted:
            if stats is None:
                results.append(_entry(check, table.name, None, [], error))
            else:
                found = _null_columns(stats) if check == "null_columns" else _constants(stats)
                results.append(_entry(check, table.name, len(found), found[:EXAMPLE_LIMIT]))
    return results


def _orphans(engine: Engine, table: Table, referred: Table, pairs: list, run: RunQuery):
    parent = referred.alias("parent")
    on = and_(*[child == parent.c[ref.name] for child, ref in pairs])
    where = and_(
        *[child.isnot(None) for child, _ in pairs],
        parent.c[pairs[0][1].name].is_(None),
    )
    joined = table.outerjoin(parent, on)
    count = select(func.count().label("violations")).select_from(joined).where(where)
    # The primary key makes each example row findable; then the dangling keys.
    pk = list(table.primary_key.columns)
    shown = pk + [c for c, _ in pairs if c.name not in {k.name for k in pk}]
    examples = select(*shown).select_from(joined).where(where).limit(EXAMPLE_LIMIT)
    return _count_and_examples(engine, count, examples, run)


def _duplicates(engine: Engine, table: Table, names: list[str], run: RunQuery):
    cols = [table.c[n] for n in names]
    # NULLs never collide under a UNIQUE constraint, so they are left out here too.
    groups = (
        select(*cols, func.count().label("duplicate_count"))
        .where(and_(*[c.isnot(None) for c in cols]))
        .group_by(*cols)
        .having(func.count() > 1)
    )
    count = select(func.count().label("violations")).select_from(groups.subquery())
    examples = groups.order_by(func.count().desc()).limit(EXAMPLE_LIMIT)
    return _count_and_examples(engine, count, examples, run)


def _count_and_examples(engine: Engine, count, examples, run: RunQuery):
    total = int(run(_compile(engine, count), 1)["rows"][0][0] or 0)
    if total == 0:
        return 0, []
    out = run(_compile(engine, examples), EXAMPLE_LIMIT)
    return total, [dict(zip(out["columns"], row, strict=False)) for row in out["rows"]]


def _column_stats(engine: Engine, table: Table, run: RunQuery) -> list[dict[str, Any]]:
    """Row count, non-null count, distinct count and min per column.

    Wide tables are split into chunks so no single SELECT carries hundreds
    of aggregates.
    """
    columns = list(table.columns)
    stats: list[dict[str, Any]] = []
    for start in range(0, len(columns), MAX_PROFILE_COLUMNS):
        chunk = columns[start:start + MAX_PROFILE_COLUMNS]
        selected = [func.count().label("n_rows")]
        for i, col in enumerate(chunk):
            selected.append(func.count(col).label(f"c{i}_nonnull"))
            if isinstance(col.type, COMPARABLE_TYPES):
                selected.append(func.count(distinct(col)).label(f"c{i}_distinct"))
            if isinstance(col.type, ORDERABLE_TYPES):
                selected.append(func.min(col).label(f"c{i}_min"))
            elif isinstance(col.type, Boolean):
                # PostgreSQL has no MIN(boolean); 1 when any value is true.
                selected.append(func.max(case((col == true(), 1), else_=0)).label(f"c{i}_true"))
        out = run(_compile(engine, select(*selected).select_from(table)), 1)
        row = dict(zip(out["columns"], out["rows"][0], strict=False))
        rows = int(row["n_rows"] or 0)
        for i, col in enumerate(chunk):
            distinct_count = row.get(f"c{i}_distinct")
            any_true = row.get(f"c{i}_true")
            stats.append({
                "column": col.name,
                "rows": rows,
                "nonnull": int(row[f"c{i}_nonnull"] or 0),
                "distinct": None if distinct_count is None else int(distinct_count),
                "value": row.get(f"c{i}_min") if any_true is None else bool(any_true),
            })
    return stats


def _null_columns(stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Columns that are NULL on every row of a non-empty table."""
    return [{"column": s["column"]} for s in stats if s["rows"] > 0 and s["nonnull"] == 0]


def _constants(stats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Columns whose non-null values are all the same, over more than one row."""
    return [
        {"column": s["column"], "value": s["value"], "null_count": s["rows"] - s["nonnull"]}
        for s in stats
        if s["rows"] > 1 and s["distinct"] == 1
    ]


def _guarded(check: str, target: str, fn: Callable[[], tuple[int, list]]) -> dict[str, Any]:
    try:
        violations, examples = fn()
    except Exception as e:
        return _entry(check, target, None, [], _message(e))
    return _entry(check, target, violations, examples)


def _entry(check: str, target: str, violations: int | None, examples: list,
           error: str | None = None) -> dict[str, Any]:
    return {"check": check, "target": target, "violations": violations,
            "examples": examples, "error": error}


def _message(e: Exception) -> str:
    text = str(e)
    return text if text else type(e).__name__
