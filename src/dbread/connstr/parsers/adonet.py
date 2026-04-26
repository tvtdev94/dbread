"""ADO.NET / C# key=value;key=value connection string parser."""

from __future__ import annotations

from dbread.connstr.parsers.adonet_tokenizer import (
    check_blocked,
    extract_host_port,
    tokenize,
)
from dbread.connstr.types import Dialect, ParsedConn

# Port → dialect heuristic
_PORT_DIALECT: dict[int, str] = {
    5432: "postgres",
    3306: "mysql",
    1433: "mssql",
    1521: "oracle",
    27017: "mongodb",
}

# Re-export tokenizer helpers so odbc.py can keep its existing import path
_tokenize = tokenize
_check_blocked = check_blocked
_extract_host_port = extract_host_port


def _infer_dialect(tokens: dict[str, str], dialect_hint: str | None) -> Dialect:
    """Determine dialect from available evidence."""
    if dialect_hint:
        return dialect_hint  # type: ignore[return-value]

    # Key-presence heuristics
    if "initial catalog" in tokens:
        return "mssql"
    if "service name" in tokens or "sid" in tokens:
        return "oracle"

    # Port heuristic
    raw_port = tokens.get("port")
    if raw_port and raw_port.isdigit():
        guessed = _PORT_DIALECT.get(int(raw_port))
        if guessed:
            return guessed  # type: ignore[return-value]

    # Server value embedded port (host,1433 or host:1433)
    server_val = (
        tokens.get("server")
        or tokens.get("data source")
        or tokens.get("host")
        or ""
    )
    for sep in (",", ":"):
        if sep in server_val:
            parts = server_val.rsplit(sep, 1)
            if parts[-1].strip().isdigit():
                guessed = _PORT_DIALECT.get(int(parts[-1].strip()))
                if guessed:
                    return guessed  # type: ignore[return-value]

    # Fallback: ADO.NET is most common for MSSQL
    return "mssql"


def parse(raw: str, *, dialect_hint: str | None = None) -> ParsedConn:
    """Parse an ADO.NET connection string."""
    tokens = tokenize(raw)
    check_blocked(tokens)

    dialect = _infer_dialect(tokens, dialect_hint)

    # Extract host (try multiple key variants)
    host: str | None = None
    port: int | None = None
    raw_server = (
        tokens.get("server")
        or tokens.get("data source")
        or tokens.get("host")
        or tokens.get("address")
        or tokens.get("addr")
        or tokens.get("network address")
    )
    if raw_server:
        host, port = extract_host_port(raw_server)

    # Explicit port key overrides port extracted from server value
    if "port" in tokens and tokens["port"].isdigit():
        port = int(tokens["port"])

    database = tokens.get("database") or tokens.get("initial catalog")
    user = (
        tokens.get("user id")
        or tokens.get("uid")
        or tokens.get("username")
        or tokens.get("user")
    )
    password = tokens.get("password") or tokens.get("pwd")

    # Collect extra params — everything not mapped to core fields
    known_consumed = {
        "server", "data source", "host", "address", "addr", "network address",
        "port", "database", "initial catalog",
        "user id", "uid", "username", "user",
        "password", "pwd",
        "trusted_connection", "trusted connection",
        "integrated security", "integratedsecurity",
    }
    params: dict[str, str] = {k: v for k, v in tokens.items() if k not in known_consumed}

    return ParsedConn(
        format="adonet",
        dialect=dialect,  # type: ignore[arg-type]
        host=host or None,
        port=port,
        database=database or None,
        user=user or None,
        password=password or None,
        params=params,
        raw=raw,
    )
