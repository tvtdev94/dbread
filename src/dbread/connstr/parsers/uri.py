"""URI / URL format parser (postgresql://, mysql://, mongodb://, etc.)."""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote, urlparse

from dbread.connstr.types import ParsedConn, UnsupportedConnString

# Map lower-cased scheme (after stripping +driver suffix) to Dialect
_SCHEME_MAP: dict[str, str] = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mysql": "mysql",
    "mssql": "mssql",
    "sqlserver": "mssql",
    "oracle": "oracle",
    "clickhouse": "clickhouse",
    "clickhousedb": "clickhouse",
    "mongodb": "mongodb",
    "sqlite": "sqlite",
    "duckdb": "duckdb",
}


def _strip_driver_suffix(scheme: str) -> str:
    """Remove +driver portion from scheme, e.g. postgresql+psycopg2 → postgresql."""
    return scheme.split("+")[0].lower()


def parse(raw: str, *, dialect_hint: str | None = None) -> ParsedConn:
    """Parse a URI-format connection string into ParsedConn.

    Raises UnsupportedConnString for Oracle TNS descriptors.
    Raises ValueError for unrecognised schemes.
    """
    parsed = urlparse(raw)
    base_scheme = _strip_driver_suffix(parsed.scheme)
    dialect = _SCHEME_MAP.get(base_scheme)
    if dialect is None:
        if dialect_hint:
            dialect = dialect_hint  # type: ignore[assignment]
        else:
            raise ValueError(f"Unrecognised URI scheme: {parsed.scheme!r}")

    host: str | None = parsed.hostname or None
    port: int | None = parsed.port or None
    # Path strip leading /
    path = parsed.path.lstrip("/") or None
    user: str | None = unquote(parsed.username) if parsed.username else None
    password: str | None = unquote(parsed.password) if parsed.password else None

    # Oracle TNS descriptor in path/netloc — block it
    if dialect == "oracle" and path and path.startswith("("):
        raise UnsupportedConnString(
            "Oracle TNS descriptor detected",
            hint=(
                "Oracle TNS descriptors cannot be auto-converted. "
                "Use EZ Connect format: oracle://user:pwd@host:1521/SERVICE_NAME"
            ),
        )

    # Detect duplicate query keys — params is dict[str, str] so duplicates
    # would be silently dropped. Warn so user can fix manually if relevant.
    raw_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    seen: set[str] = set()
    duplicates: list[str] = []
    for k, _ in raw_pairs:
        if k in seen and k not in duplicates:
            duplicates.append(k)
        seen.add(k)
    if duplicates:
        import sys as _sys  # noqa: PLC0415

        print(
            f"WARNING: Duplicate query keys ({', '.join(duplicates)}) — "
            "only the last value of each will be used. "
            "If you need multi-valued params, edit config.yaml manually.",
            file=_sys.stderr,
        )
    params: dict[str, str] = dict(raw_pairs)

    # mongodb+srv: mark srv flag; format stays "uri" here — cloud.py upgrades it
    if parsed.scheme.lower().startswith("mongodb+srv"):
        params.setdefault("srv", "true")

    return ParsedConn(
        format="uri",
        dialect=dialect,  # type: ignore[arg-type]
        host=host,
        port=port,
        database=path,
        user=user,
        password=password,
        params=params,
        raw=raw,
    )


def parse_with_scheme(rewritten: str, *, dialect_hint: str | None = None) -> ParsedConn:
    """Parse after the caller has already rewritten the scheme.

    Thin wrapper used by jdbc.py so it can feed 'postgresql://...' directly.
    """
    return parse(rewritten, dialect_hint=dialect_hint)
