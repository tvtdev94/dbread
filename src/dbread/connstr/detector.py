"""Connection-string format detector with priority-ordered dispatch."""

from __future__ import annotations

from pathlib import Path

from dbread.connstr.parsers import adonet, cloud, filepath, jdbc, odbc, uri
from dbread.connstr.types import ParsedConn, UnknownFormat

# ADO.NET server-key indicators (lower-cased)
_ADONET_SERVER_KEYS = ("server=", "data source=", "host=", "datasource=")


def detect_and_parse(raw: str, *, dialect_hint: str | None = None) -> ParsedConn:
    """Detect the format of *raw* and return a ParsedConn.

    Priority order (most-specific first):
    1. JDBC          — starts with jdbc:
    2. Cloud markers — mongodb+srv:// or md:
    3. Generic URI   — contains :// (re-routes to cloud if *.clickhouse.cloud)
    4. ODBC          — contains Driver={
    5. ADO.NET       — key=value; with server/data source/host key
    6. File path     — known extension or :memory:
    7. Raise UnknownFormat

    Strips leading BOM and surrounding whitespace before matching.
    """
    # Strip BOM (U+FEFF) and surrounding whitespace
    s = raw.strip().lstrip("﻿")

    lower = s.lower()

    # 1. JDBC — most specific; must come before URI check
    if lower.startswith("jdbc:"):
        return jdbc.parse(s, dialect_hint=dialect_hint)

    # 2. Cloud markers that are unambiguous
    if lower.startswith("mongodb+srv://") or lower.startswith("md:"):
        return cloud.parse(s, dialect_hint=dialect_hint)

    # 3. Generic URI — anything with :// that is not an ODBC string
    if "://" in s and not lower.startswith("driver"):
        parsed = uri.parse(s, dialect_hint=dialect_hint)
        # Re-route to cloud if hostname matches ClickHouse Cloud pattern
        if parsed.host and ".clickhouse.cloud" in parsed.host.lower():
            return cloud.from_uri_parsed(parsed)
        return parsed

    # 4. ODBC — Driver={...} key present
    if "driver={" in lower:
        return odbc.parse(s, dialect_hint=dialect_hint)

    # 5. ADO.NET — has = and ; and at least one server-key indicator
    if (
        "=" in s
        and ";" in s
        and any(key in lower for key in _ADONET_SERVER_KEYS)
    ):
        return adonet.parse(s, dialect_hint=dialect_hint)

    # 6. File path / :memory:
    if s == ":memory:":
        return filepath.parse(s, dialect_hint=dialect_hint)

    suffix = Path(s).suffix.lower()
    if suffix in {".db", ".sqlite", ".sqlite3", ".duckdb"}:
        return filepath.parse(s, dialect_hint=dialect_hint)

    raise UnknownFormat(
        f"Cannot identify connection-string format for input "
        f"(first 30 chars): {s[:30]!r}"
    )
