"""Live connection health check + smart error classification.

Shared by:
  - `dbread add` wizard (test before save)
  - `dbread doctor` (per-connection health)

Lazy imports SQLAlchemy/pymongo so importing this module is cheap on
CLI startup; only `test_connection()` actually loads a driver.
"""

from __future__ import annotations

import re
import time


def test_connection(dialect: str, url: str, *, timeout_s: int = 5) -> tuple[bool, str, float]:
    """Open a real connection and run a trivial probe.

    SQL dialects: `SELECT 1` via SQLAlchemy.
    MongoDB: `admin.command("ping")`.

    Returns (ok, error_message, elapsed_ms). Errors are truncated to ~300 chars.
    """
    t0 = time.monotonic()
    try:
        if dialect == "mongodb":
            from pymongo import MongoClient  # noqa: PLC0415
            client = MongoClient(url, serverSelectionTimeoutMS=timeout_s * 1000)
            client.admin.command("ping")
            client.close()
        else:
            from sqlalchemy import create_engine, text  # noqa: PLC0415
            eng = create_engine(url, connect_args=_timeout_kwargs_for(dialect, timeout_s))
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            eng.dispose()
        return True, "", (time.monotonic() - t0) * 1000
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:300], (time.monotonic() - t0) * 1000


def _timeout_kwargs_for(dialect: str, timeout_s: int = 5) -> dict:
    """Best-effort connect-timeout kwargs per dialect (SQLAlchemy connect_args)."""
    return {
        "postgres": {"connect_timeout": timeout_s},
        "mysql": {"connect_timeout": timeout_s},
        "mssql": {"timeout": timeout_s},
    }.get(dialect, {})


# Ordered list — first matching pattern wins. Patterns are case-insensitive.
_ERROR_PATTERNS: list[tuple[re.Pattern[str], list[str]]] = [
    (
        re.compile(
            r"connection refused|could not connect|timed out|"
            r"name or service not known|no route to host|host is down",
            re.I,
        ),
        [
            "Host or port unreachable.",
            "  -> Check the URL host:port in ~/.dbread/.env",
            "  -> Verify the DB server is running and the port is open",
            "  -> If on a corporate network, check VPN/firewall",
        ],
    ),
    (
        re.compile(
            r"authentication failed|password authentication|access denied|"
            r"not authorized|login failed|invalid credentials",
            re.I,
        ),
        [
            "Authentication failed (wrong username or password).",
            "  -> Check user/password in ~/.dbread/.env",
            "  -> Verify the user exists in the DB and has read permission",
        ],
    ),
    (
        re.compile(r"database .+ does not exist|unknown database|no such database", re.I),
        [
            "Database name doesn't exist on the server.",
            "  -> Check the database name (last path segment of the URL)",
        ],
    ),
    (
        re.compile(r"\bssl\b|sslmode|tls handshake|ssl required", re.I),
        [
            "SSL/TLS configuration issue.",
            "  -> PostgreSQL: append `?sslmode=require` (or `?sslmode=verify-full`)",
            "  -> MongoDB: append `?tls=true`",
            "  -> MySQL: append `?ssl_disabled=false&ssl_verify_cert=false`",
        ],
    ),
    (
        re.compile(r"no module named|modulenotfounderror", re.I),
        [
            "Driver Python package not installed.",
            "  -> Run: dbread add-extra <extra-name>  (see `dbread list-extras`)",
        ],
    ),
]


def classify_error(err: str) -> list[str]:
    """Return human-friendly fix hint lines based on error text."""
    for pattern, hint in _ERROR_PATTERNS:
        if pattern.search(err):
            return hint
    return [
        "Generic connection error.",
        "  -> Check the URL in ~/.dbread/.env",
        "  -> Try `psql`/`mysql`/`mongosh` with the same URL to isolate",
    ]
