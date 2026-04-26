"""ODBC connection string parser (Driver={...};Server=...;...)."""

from __future__ import annotations

import re

from dbread.connstr.parsers.adonet_tokenizer import check_blocked, extract_host_port, tokenize
from dbread.connstr.types import Dialect, ParsedConn

# Driver name patterns → dialect
_DRIVER_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # MSSQL — must come before generic "SQL Server" match
    (re.compile(r"odbc driver \d+ for sql server", re.I), "mssql"),
    (re.compile(r"sql server", re.I), "mssql"),
    (re.compile(r"sql native client", re.I), "mssql"),
    # MySQL
    (re.compile(r"mysql(?: odbc)?(?: \d+\.\d+)? (?:unicode|ansi )?driver", re.I), "mysql"),
    (re.compile(r"mysql", re.I), "mysql"),
    # PostgreSQL
    (re.compile(r"postgresql(?: odbc)? driver", re.I), "postgres"),
    (re.compile(r"postgresql", re.I), "postgres"),
    (re.compile(r"psqlodbc", re.I), "postgres"),
    # Oracle
    (re.compile(r"oracle", re.I), "oracle"),
]

# Port → dialect fallback (shared with adonet)
_PORT_DIALECT: dict[int, str] = {
    5432: "postgres",
    3306: "mysql",
    1433: "mssql",
    1521: "oracle",
    27017: "mongodb",
}


def _extract_driver(raw: str) -> tuple[str | None, str]:
    """Return (driver_name, remainder_without_driver_token).

    Handles Driver={...} anywhere in the string, case-insensitive.
    """
    pattern = re.compile(r"(?i)driver=\{([^}]*)\}\s*;?")
    match = pattern.search(raw)
    if match:
        driver_name = match.group(1).strip()
        remainder = raw[: match.start()] + raw[match.end() :]
        return driver_name, remainder
    return None, raw


def _infer_dialect_from_driver(driver_name: str) -> str | None:
    for pat, dialect in _DRIVER_PATTERNS:
        if pat.search(driver_name):
            return dialect
    return None


def parse(raw: str, *, dialect_hint: str | None = None) -> ParsedConn:
    """Parse an ODBC connection string."""
    driver_name, remainder = _extract_driver(raw)
    tokens = tokenize(remainder)
    check_blocked(tokens)

    # Determine dialect: driver name → port → hint → mssql fallback
    dialect: Dialect
    if driver_name:
        guessed = _infer_dialect_from_driver(driver_name)
        if guessed:
            dialect = guessed  # type: ignore[assignment]
        elif dialect_hint:
            dialect = dialect_hint  # type: ignore[assignment]
        else:
            dialect = "mssql"
    elif dialect_hint:
        dialect = dialect_hint  # type: ignore[assignment]
    else:
        dialect = "mssql"

    # Host / port
    host: str | None = None
    port: int | None = None
    raw_server = (
        tokens.get("server")
        or tokens.get("data source")
        or tokens.get("host")
        or tokens.get("address")
    )
    if raw_server:
        host, port = extract_host_port(raw_server)

    if "port" in tokens and tokens["port"].isdigit():
        port = int(tokens["port"])

    # If still no dialect clue from driver, try port
    if dialect == "mssql" and port and not dialect_hint:
        guessed_from_port = _PORT_DIALECT.get(port)
        if guessed_from_port:
            dialect = guessed_from_port  # type: ignore[assignment]

    database = tokens.get("database") or tokens.get("initial catalog")

    # ODBC uses UID / PWD as standard keys
    user = (
        tokens.get("uid")
        or tokens.get("user id")
        or tokens.get("username")
        or tokens.get("user")
    )
    password = tokens.get("pwd") or tokens.get("password")

    # Extra params: everything not consumed above + driver name
    known_consumed = {
        "server", "data source", "host", "address",
        "port", "database", "initial catalog",
        "uid", "user id", "username", "user",
        "pwd", "password",
        "trusted_connection", "trusted connection",
        "integrated security", "integratedsecurity",
    }
    params: dict[str, str] = {k: v for k, v in tokens.items() if k not in known_consumed}

    # Store driver name in params for converter phase
    if driver_name:
        params["driver"] = driver_name

    return ParsedConn(
        format="odbc",
        dialect=dialect,
        host=host or None,
        port=port,
        database=database or None,
        user=user or None,
        password=password or None,
        params=params,
        raw=raw,
    )
