"""Exact data-integrity checks over a whole collection.

Each check is one aggregate through `query`, so the guard, cost guard,
statement timeout, rate limit and audit apply to it. A failing check reports
its own `error` and leaves the others running.

- orphans: documents whose `field` matches nothing in `to.to_field`
  (`$lookup` anti-join; missing/null references are not orphans).
- duplicates: values of `unique_columns`, taken together, held by more than
  one document (documents with any of those fields null/missing are ignored).
- null_columns / constant_columns: fields seen in a `$sample` that are null
  everywhere / hold a single value everywhere, from one `$group`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from .execute import raise_tool_error
from .inspect import collection_infos
from .query import MongoQueryMixin
from .schema import flatten_doc

CHECKS = ("orphans", "duplicates", "null_columns", "constant_columns")
MAX_EXAMPLES = 5
# Fields per null/constant `$group`; each costs three accumulators.
MAX_PROFILED_FIELDS = 100

T = TypeVar("T")


def _field_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value or any(
        not part or part.startswith("$") for part in value.split(".")
    ):
        raise_tool_error(f"invalid_input: {what} must be a field path, got {value!r}")
    return value


def _parse_references(references: Any) -> list[dict[str, str]]:
    if references is None:
        return []
    if not isinstance(references, list):
        raise_tool_error("invalid_input: references must be a list")
    out = []
    for ref in references:
        if not isinstance(ref, dict):
            raise_tool_error("invalid_input: each reference must be an object")
        to = ref.get("to")
        if not isinstance(to, str) or not to or "." in to or to.startswith("$"):
            raise_tool_error(
                "invalid_input: reference 'to' must be a collection in the same database; "
                f"names containing '.' or starting with '$' are not supported, got {to!r}"
            )
        out.append({
            "field": _field_name(ref.get("field"), "reference 'field'"),
            "to": to,
            "to_field": _field_name(ref.get("to_field", "_id"), "reference 'to_field'"),
        })
    return out


def _counted(rows: list[dict]) -> tuple[int, list]:
    """Unpack the `{total: [{n}], examples: [...]}` facet every check ends with."""
    facet = rows[0] if rows else {}
    total = facet.get("total") or [{}]
    return int(total[0].get("n", 0)), list(facet.get("examples") or [])


def _facet(examples: list[dict]) -> dict:
    return {"$facet": {"total": [{"$count": "n"}],
                       "examples": [*examples, {"$limit": MAX_EXAMPLES}]}}


def _same_value(lo: Any, hi: Any) -> bool:
    """`$min == $max` compared as encoded BSON.

    Python equality conflates values BSON keeps apart (`1 == True`, also
    nested as `[1] == [True]`), so a field holding both would read as
    constant. Equal encodings mean the same BSON type and value.
    """
    import bson

    return bson.encode({"v": lo}) == bson.encode({"v": hi})


def _isolated(fn: Callable[[], T]) -> tuple[T | None, str | None]:
    """Run one check; a refusal or server error becomes that check's `error`."""
    from ..tools import ToolError

    try:
        return fn(), None
    except ToolError as e:
        return None, str(e)


def _entry(
    check: str, target: str, found: tuple[int, list] | None = None, error: str | None = None,
) -> dict[str, Any]:
    # A check that could not run has no count; 0 would read as "clean".
    if error is not None or found is None:
        return {"check": check, "target": target, "violations": None,
                "examples": [], "error": error}
    violations, examples = found
    return {"check": check, "target": target, "violations": violations,
            "examples": examples[:MAX_EXAMPLES], "error": None}


class MongoIntegrityMixin(MongoQueryMixin):
    def check_integrity(
        self,
        connection: str,
        table: str,
        checks: list[str] | None = None,
        references: list[dict] | None = None,
        unique_columns: list[str] | None = None,
        database: str | None = None,
    ) -> dict[str, Any]:
        refs = _parse_references(references)
        unique = [_field_name(c, "unique_columns entry") for c in unique_columns or []]
        if checks is not None and not checks:
            raise_tool_error("invalid_input: checks must not be empty")
        # Defaulted checks leave out duplicates when there is no key to check.
        wanted = (
            [c for c in CHECKS if c != "duplicates" or unique] if checks is None
            else list(checks)
        )
        unknown = [c for c in wanted if c not in CHECKS]
        if unknown:
            raise_tool_error(f"invalid_input: unknown check: {unknown[0]}")

        op = {"listCollections": 1}
        db = self._resolve(connection, database, op)
        ms = self._max_time_ms(connection)
        names = {c["name"] for c in self._metadata(
            connection, op, lambda: collection_infos(db, ms),
            db_name=self._explicit(database, db),
        )}
        if table not in names:
            raise_tool_error(f"unknown_table: {table}")

        results: list[dict[str, Any]] = []
        if "orphans" in wanted:
            for ref in refs:
                target = f"{ref['field']} -> {ref['to']}.{ref['to_field']}"
                if ref["to"] not in names:
                    results.append(_entry("orphans", target, error=f"unknown_table: {ref['to']}"))
                    continue
                found, error = _isolated(
                    lambda r=ref: self._orphans(connection, table, r, database)
                )
                results.append(_entry("orphans", target, found, error))
        if "duplicates" in wanted and not unique:
            results.append(_entry("duplicates", "", error="unique_columns required"))
        elif "duplicates" in wanted:
            found, error = _isolated(lambda: self._duplicates(connection, table, unique, database))
            results.append(_entry("duplicates", ",".join(unique), found, error))
        profiled = [c for c in ("null_columns", "constant_columns") if c in wanted]
        if profiled:
            # Both come from one `$group`, so they succeed or fail together.
            found, error = _isolated(lambda: self._null_and_constant(connection, table, database))
            for check in profiled:
                hits = found[check] if found else []
                results.append(_entry(check, table, (len(hits), hits), error))
        return {"table": table, "checks": results}

    def _orphans(
        self, connection: str, table: str, ref: dict, database: str | None,
    ) -> tuple[int, list]:
        field = ref["field"]
        return _counted(self._aggregate_docs(connection, table, [
            {"$match": {field: {"$ne": None}}},
            {"$lookup": {"from": ref["to"], "localField": field,
                         "foreignField": ref["to_field"], "as": "_dbread_ref"}},
            {"$match": {"_dbread_ref": {"$size": 0}}},
            _facet([{"$project": {"_id": 1, field: 1}}]),
        ], database, max_rows=1, allow_disk_use=True))

    def _duplicates(
        self, connection: str, table: str, cols: list[str], database: str | None,
    ) -> tuple[int, list]:
        # Positional keys: `$group` refuses dotted names inside its `_id`.
        key = {f"k{i}": f"${c}" for i, c in enumerate(cols)}
        n, groups = _counted(self._aggregate_docs(connection, table, [
            {"$match": {c: {"$ne": None} for c in cols}},
            {"$group": {"_id": key, "n": {"$sum": 1}}},
            {"$match": {"n": {"$gt": 1}}},
            _facet([{"$sort": {"n": -1}}]),
        ], database, max_rows=1, allow_disk_use=True))
        examples = [
            {**{c: g["_id"].get(f"k{i}") for i, c in enumerate(cols)}, "duplicate_count": g["n"]}
            for g in groups
        ]
        return n, examples

    def _null_and_constant(
        self, connection: str, table: str, database: str | None,
    ) -> dict[str, list[dict]]:
        sample = self._sample_docs(connection, table, self._sample_size(connection), database)
        fields = sorted({
            path for doc in sample for path, _ in flatten_doc(doc)
            if path != "_id" and not any(p.startswith("$") or not p for p in path.split("."))
        })[:MAX_PROFILED_FIELDS]
        found: dict[str, list[dict]] = {"null_columns": [], "constant_columns": []}
        if not fields:
            return found
        group: dict[str, Any] = {"_id": None, "total": {"$sum": 1}}
        for i, f in enumerate(fields):
            present = {"$cond": [{"$eq": [{"$ifNull": [f"${f}", None]}, None]}, 0, 1]}
            group.update({f"n{i}": {"$sum": present},
                          f"lo{i}": {"$min": f"${f}"}, f"hi{i}": {"$max": f"${f}"}})
        rows = self._aggregate_docs(
            connection, table, [{"$group": group}], database, max_rows=1, allow_disk_use=True,
        )
        if not rows:
            return found
        row = rows[0]
        total = int(row.get("total", 0))
        for i, f in enumerate(fields):
            if int(row.get(f"n{i}", 0)) == 0:
                found["null_columns"].append({"column": f})
            elif total > 1 and _same_value(row.get(f"lo{i}"), row.get(f"hi{i}")):
                found["constant_columns"].append({"column": f, "value": row.get(f"lo{i}")})
        return found
