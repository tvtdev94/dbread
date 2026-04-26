"""File-path connection string parser for SQLite and DuckDB."""

from __future__ import annotations

from pathlib import Path

from dbread.connstr.types import Dialect, ParsedConn, UnknownFormat

# Extension → dialect
_SUFFIX_MAP: dict[str, str] = {
    ".db": "sqlite",
    ".sqlite": "sqlite",
    ".sqlite3": "sqlite",
    ".duckdb": "duckdb",
}


def parse(raw: str, *, dialect_hint: str | None = None) -> ParsedConn:
    """Parse a file path or :memory: into ParsedConn.

    - Resolves relative paths to absolute.
    - Converts backslashes to forward slashes (SQLAlchemy requirement).
    - :memory: returns dialect from hint (defaults to 'sqlite').
    """
    stripped = raw.strip()

    # In-memory shorthand
    if stripped == ":memory:":
        dialect: Dialect = dialect_hint or "sqlite"  # type: ignore[assignment]
        return ParsedConn(
            format="filepath",
            dialect=dialect,
            database=":memory:",
            raw=raw,
        )

    p = Path(stripped)
    suffix = p.suffix.lower()

    if suffix not in _SUFFIX_MAP and dialect_hint is None:
        raise UnknownFormat(
            f"File extension {suffix!r} is not a recognised database file type. "
            "Supported: .db, .sqlite, .sqlite3, .duckdb"
        )

    inferred_dialect = _SUFFIX_MAP.get(suffix)
    final_dialect: Dialect = (
        inferred_dialect or dialect_hint  # type: ignore[assignment]
    )

    # Resolve to absolute path
    resolved = p.resolve()
    # Convert to forward slashes for cross-platform SQLAlchemy compat
    db_path = resolved.as_posix()

    return ParsedConn(
        format="filepath",
        dialect=final_dialect,
        database=db_path,
        raw=raw,
    )
