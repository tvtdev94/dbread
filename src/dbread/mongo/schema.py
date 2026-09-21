"""Sample-based schema inference for MongoDB collections.

Mongo is schemaless; we sample `n` docs (server-side via `$sample`) and
summarize observed paths with their types and frequency. Nested docs are
flattened via dotted paths (`address.city`); arrays are typed as
`array<T|U>` using up to 5 element samples.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import datetime
from typing import Any


def flatten_doc(doc: dict, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Yield (path, value) pairs. Nested dicts → dotted paths; arrays kept as-is."""
    for key, val in doc.items():
        if key == "_id" and prefix == "":
            yield ("_id", val)
            continue
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(val, dict):
            yield from flatten_doc(val, path)
        else:
            yield (path, val)


def bson_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        inner = {bson_type(x) for x in value[:5]} or {"unknown"}
        return f"array<{'|'.join(sorted(inner))}>"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def docs_to_rows(docs: list[dict], cap: int) -> tuple[list[list], list[str]]:
    """Flatten a list of top-level dicts to (rows, columns) using the union of keys.

    Missing keys become `None`. Non-dict items contribute an all-`None` row. Respects `cap`.
    """
    cols: list[str] = []
    trimmed = docs[:cap]
    for d in trimmed:
        if isinstance(d, dict):
            for k in d:
                if k not in cols:
                    cols.append(k)
    rows = [
        [d.get(k) if isinstance(d, dict) else None for k in cols]
        for d in trimmed
    ]
    return rows, cols


def _distinct_count(values: list) -> int:
    """Count distinct values, tolerating unhashable documents and arrays."""
    seen: set = set()
    for value in values:
        try:
            seen.add(value)
        except TypeError:
            seen.add(repr(value))
    return len(seen)


def _comparable_kind(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, datetime):
        return "datetime"
    return None


def _min_max(values: list) -> tuple[Any, Any]:
    """MIN/MAX only when every value shares one comparable kind.

    Mongo lets one field hold a string in some documents and a number in
    others; ordering across those is meaningless, so report nothing.
    """
    kinds = {_comparable_kind(v) for v in values}
    if len(kinds) != 1 or None in kinds:
        return None, None
    return min(values), max(values)


def profile_docs(sample: list[dict], requested: list[str] | None = None) -> list[dict]:
    """Per-field null / distinct / min / max over an in-memory sample.

    The sample is already fetched for schema inference, so the statistics
    cost nothing extra at the server.
    """
    total = len(sample)
    present: dict[str, list] = defaultdict(list)
    types_by_path: dict[str, set[str]] = defaultdict(set)
    for doc in sample:
        for path, value in flatten_doc(doc):
            types_by_path[path].add(bson_type(value))
            if value is not None:
                present[path].append(value)

    paths = sorted(types_by_path) if requested is None else requested
    fields: list[dict] = []
    for path in paths:
        values = present.get(path, [])
        nulls = total - len(values)
        entry: dict[str, Any] = {
            "name": path,
            "types": sorted(types_by_path.get(path, set())),
            "null_count": nulls,
            "null_pct": round(nulls / total * 100, 1) if total else 0.0,
            "distinct_count": _distinct_count(values),
        }
        low, high = _min_max(values)
        if low is not None:
            entry["min"], entry["max"] = low, high
        fields.append(entry)
    return fields


def infer_schema(sample: list[dict], sample_size: int) -> list[dict]:
    """Return a list of field descriptors inferred from the sample."""
    types_by_path: dict[str, Counter] = defaultdict(Counter)
    count_by_path: dict[str, int] = defaultdict(int)
    total = len(sample)
    for doc in sample:
        seen: set[str] = set()
        for path, val in flatten_doc(doc):
            types_by_path[path][bson_type(val)] += 1
            if path not in seen:
                count_by_path[path] += 1
                seen.add(path)
    fields: list[dict] = []
    for path, counter in sorted(types_by_path.items()):
        freq = round(count_by_path[path] / total, 2) if total else 0.0
        fields.append({
            "name": path,
            "types": sorted(counter.keys()),
            "frequency": freq,
            "pk": path == "_id",
        })
    return fields
