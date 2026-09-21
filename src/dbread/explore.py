"""Statement builders for the `sample_table` and `profile_table` tools.

These tools exist because hand-rolling the same thing costs an agent one
`describe_table` round plus several dialect-specific queries against a
per-minute rate budget — and the sampling policy that keeps a profile cheap
is exactly the part that gets skipped.

Everything here builds SQLAlchemy Core expressions against a reflected table,
so identifiers are quoted by the dialect rather than pasted into a string.
The compiled text then goes back through the normal guard → rate → audit
path like any other query.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Table, distinct, func, select
from sqlalchemy import types as sqltypes
from sqlalchemy.engine import Engine

# Types that have an equality operator on every backend, so COUNT(DISTINCT x)
# is safe. PostgreSQL has none for `json`, `xml` or the geometric types, and
# the resulting error takes down the whole profile rather than one column.
COMPARABLE_TYPES = (
    sqltypes.Numeric,
    sqltypes.Integer,
    sqltypes.Float,
    sqltypes.String,
    sqltypes.Date,
    sqltypes.DateTime,
    sqltypes.Time,
    sqltypes.Boolean,
)

# Types that additionally have an ordering, so MIN/MAX means something.
# Boolean is comparable but PostgreSQL has no `min(boolean)`.
ORDERABLE_TYPES = tuple(t for t in COMPARABLE_TYPES if t is not sqltypes.Boolean)

# Each column contributes up to four aggregates to one SELECT. A wide
# analytics table would otherwise build a query with hundreds of them.
MAX_PROFILE_COLUMNS = 64

# Column names that usually carry row recency, most specific first.
_RECENCY_NAMES = (
    "created_at", "createdat", "created", "inserted_at",
    "updated_at", "updatedat", "modified_at", "timestamp", "ts",
)


@dataclass(frozen=True)
class ColumnPlan:
    """Which aggregates were requested for one column, and their labels."""

    name: str
    type_name: str
    nonnull_label: str
    distinct_label: str | None
    min_label: str | None
    max_label: str | None


def reflect(engine: Engine, table: str, schema: str | None) -> Table:
    from sqlalchemy import MetaData

    return Table(table, MetaData(), autoload_with=engine, schema=schema)


def recency_column(table: Table) -> str | None:
    """Best column to sort by when showing 'the latest rows'.

    A named timestamp column beats the primary key, because a UUID or random
    primary key sorts into an order that has nothing to do with time.
    """
    by_name = {c.name.lower(): c for c in table.columns}
    for candidate in _RECENCY_NAMES:
        column = by_name.get(candidate)
        if column is not None and isinstance(
            column.type, sqltypes.Date | sqltypes.DateTime | sqltypes.Numeric
        ):
            return column.name
    pk = list(table.primary_key.columns)
    orderable_pk = sqltypes.Integer | sqltypes.Date | sqltypes.DateTime
    if len(pk) == 1 and isinstance(pk[0].type, orderable_pk):
        return pk[0].name
    return None


def sample_sql(engine: Engine, table: Table, limit: int, order_by: str | None) -> str:
    stmt = select(table)
    if order_by is not None:
        stmt = stmt.order_by(table.c[order_by].desc())
    return _compile(engine, stmt.limit(limit))


def profile_sql(
    engine: Engine,
    table: Table,
    columns: list[str] | None,
    sample_size: int,
) -> tuple[str, list[ColumnPlan], str]:
    """Build the one-row profile query.

    Aggregating over a bounded subquery rather than the whole table keeps the
    cost predictable on every dialect — no TABLESAMPLE, no planner surprises.
    Returns the SQL, the per-column plan needed to read the row back, and the
    label holding the sampled row count.
    """
    chosen = _pick_columns(table, columns)
    if not chosen:
        raise ValueError("no profilable columns on this table")
    if len(chosen) > MAX_PROFILE_COLUMNS:
        raise ValueError(
            f"{len(chosen)} columns exceeds the {MAX_PROFILE_COLUMNS} limit; "
            "pass `columns` to profile a subset"
        )

    sub = select(table).limit(sample_size).subquery()
    total_label = "n_rows"
    selected = [func.count().label(total_label)]
    plans: list[ColumnPlan] = []

    for index, column in enumerate(chosen):
        source = sub.c[column.name]
        nonnull = f"c{index}_nonnull"
        selected.append(func.count(source).label(nonnull))

        distinct_ = None
        if isinstance(column.type, COMPARABLE_TYPES):
            distinct_ = f"c{index}_distinct"
            selected.append(func.count(distinct(source)).label(distinct_))

        min_label = max_label = None
        if isinstance(column.type, ORDERABLE_TYPES):
            min_label, max_label = f"c{index}_min", f"c{index}_max"
            selected.append(func.min(source).label(min_label))
            selected.append(func.max(source).label(max_label))

        plans.append(ColumnPlan(
            name=column.name,
            type_name=str(column.type),
            nonnull_label=nonnull,
            distinct_label=distinct_,
            min_label=min_label,
            max_label=max_label,
        ))

    stmt = select(*selected).select_from(sub)
    return _compile(engine, stmt), plans, total_label


def build_profile(
    row: dict,
    plans: list[ColumnPlan],
    total_label: str,
) -> tuple[int, list[dict]]:
    """Reshape the single aggregate row into one entry per column."""
    sampled = int(row.get(total_label) or 0)
    fields = []
    for plan in plans:
        nonnull = int(row.get(plan.nonnull_label) or 0)
        nulls = sampled - nonnull
        entry = {
            "name": plan.name,
            "type": plan.type_name,
            "null_count": nulls,
            "null_pct": round(nulls / sampled * 100, 1) if sampled else 0.0,
        }
        if plan.distinct_label is not None:
            entry["distinct_count"] = int(row.get(plan.distinct_label) or 0)
        if plan.min_label is not None:
            entry["min"] = row.get(plan.min_label)
            entry["max"] = row.get(plan.max_label)
        fields.append(entry)
    return sampled, fields


def _pick_columns(table: Table, requested: list[str] | None) -> list:
    if requested is None:
        return list(table.columns)
    by_name = {c.name: c for c in table.columns}
    missing = [name for name in requested if name not in by_name]
    if missing:
        raise ValueError(f"unknown column: {missing[0]}")
    return [by_name[name] for name in requested]


def _compile(engine: Engine, stmt) -> str:
    return str(stmt.compile(engine, compile_kwargs={"literal_binds": True}))
