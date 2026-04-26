"""JDBC connection string parser (jdbc:postgresql://, jdbc:sqlserver://, etc.)."""

from __future__ import annotations

import re

from dbread.connstr.parsers import uri as uri_parser
from dbread.connstr.types import ParsedConn

# Sub-schemes that can be delegated directly to the URI parser after stripping jdbc:
_URI_DELEGATED_PREFIXES = (
    "postgresql://",
    "postgres://",
    "mysql://",
    "oracle://",
    "mongodb://",
    "mongodb+srv://",
    "duckdb://",
)


def _parse_sqlserver(raw: str, original: str) -> ParsedConn:
    """Parse jdbc:sqlserver://host:port;databaseName=...;user=...;password=..."""
    # raw is the portion after "jdbc:sqlserver://"
    # Split on first ';' to separate netloc from properties
    if ";" in raw:
        netloc, props_str = raw.split(";", 1)
    else:
        netloc, props_str = raw, ""

    # Parse host:port from netloc
    host: str | None = None
    port: int | None = None
    if netloc:
        if ":" in netloc:
            h, p = netloc.rsplit(":", 1)
            host = h or None
            port = int(p) if p.isdigit() else None
        else:
            host = netloc or None

    # Tokenize remaining semicolon-separated properties
    tokens: dict[str, str] = {}
    for pair in props_str.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, _, v = pair.partition("=")
        tokens[k.strip().lower()] = v.strip()

    database = tokens.get("databasename") or tokens.get("database") or tokens.get("initial catalog")
    user = tokens.get("user") or tokens.get("user id") or tokens.get("uid")
    password = tokens.get("password") or tokens.get("pwd")

    known = {
        "databasename", "database", "initial catalog",
        "user", "user id", "uid", "password", "pwd",
    }
    params = {k: v for k, v in tokens.items() if k not in known}

    return ParsedConn(
        format="jdbc",
        dialect="mssql",
        host=host,
        port=port,
        database=database or None,
        user=user or None,
        password=password or None,
        params=params,
        raw=original,
    )


def _parse_oracle(after_at: str, original: str) -> ParsedConn:
    """Parse both oracle thin formats:
    - @//host:port/SERVICE_NAME  (EZ Connect)
    - @host:port:SID             (old SID style)
    """
    params: dict[str, str] = {}

    # EZ Connect: @//host:port/SERVICE
    ez = re.match(r"^//([^/:]+):(\d+)/(.+)$", after_at)
    if ez:
        host, port_s, service = ez.group(1), ez.group(2), ez.group(3)
        params["service_name"] = service
        return ParsedConn(
            format="jdbc",
            dialect="oracle",
            host=host,
            port=int(port_s),
            database=service,
            params=params,
            raw=original,
        )

    # SID style: host:port:SID
    sid_match = re.match(r"^([^:]+):(\d+):(.+)$", after_at)
    if sid_match:
        host, port_s, sid = sid_match.group(1), sid_match.group(2), sid_match.group(3)
        params["sid"] = sid
        return ParsedConn(
            format="jdbc",
            dialect="oracle",
            host=host,
            port=int(port_s),
            database=sid,
            params=params,
            raw=original,
        )

    # Fallback: treat entire string as host
    return ParsedConn(
        format="jdbc",
        dialect="oracle",
        host=after_at or None,
        params=params,
        raw=original,
    )


def _parse_clickhouse(after_prefix: str, original: str) -> ParsedConn:
    """Parse jdbc:clickhouse://host:port/db — delegate to URI parser."""
    result = uri_parser.parse_with_scheme("clickhouse://" + after_prefix)
    return ParsedConn(
        format="jdbc",
        dialect="clickhouse",
        host=result.host,
        port=result.port,
        database=result.database,
        user=result.user,
        password=result.password,
        params=result.params,
        raw=original,
    )


def parse(raw: str, *, dialect_hint: str | None = None) -> ParsedConn:
    """Parse a JDBC connection string."""
    original = raw
    # Strip jdbc: prefix (case-insensitive)
    if raw.lower().startswith("jdbc:"):
        raw = raw[5:]

    lower = raw.lower()

    # --- mongodb+srv delegate to cloud parser (imported lazily to avoid circular) ---
    if lower.startswith("mongodb+srv://"):
        from dbread.connstr.parsers import cloud as cloud_parser  # noqa: PLC0415
        return cloud_parser.parse(raw, dialect_hint=dialect_hint)

    # --- URI-delegatable schemes ---
    for prefix in _URI_DELEGATED_PREFIXES:
        if lower.startswith(prefix):
            result = uri_parser.parse_with_scheme(raw, dialect_hint=dialect_hint)
            return ParsedConn(
                format="jdbc",
                dialect=result.dialect,
                host=result.host,
                port=result.port,
                database=result.database,
                user=result.user,
                password=result.password,
                params=result.params,
                raw=original,
            )

    # --- SQL Server ---
    if lower.startswith("sqlserver://"):
        return _parse_sqlserver(raw[len("sqlserver://"):], original)

    # --- Oracle thin ---
    if lower.startswith("oracle:thin:@") or lower.startswith("oracle:thin:@//"):
        after_at = raw[len("oracle:thin:@"):]
        return _parse_oracle(after_at, original)

    # --- ClickHouse ---
    if lower.startswith("clickhouse://"):
        return _parse_clickhouse(raw[len("clickhouse://"):], original)

    # --- Generic fallback: try URI parser ---
    result = uri_parser.parse_with_scheme(raw, dialect_hint=dialect_hint)
    return ParsedConn(
        format="jdbc",
        dialect=result.dialect,
        host=result.host,
        port=result.port,
        database=result.database,
        user=result.user,
        password=result.password,
        params=result.params,
        raw=original,
    )
