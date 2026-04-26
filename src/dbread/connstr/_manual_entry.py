"""Manual connection-string entry helpers for the add wizard.

Extracted from wizard.py to keep that module under 350 LOC.

Public functions:
    _offer_fallback_menu(raw, dialect_hint) -> ParsedConn | int
    _manual_url_entry(dialect_hint) -> ParsedConn
    _generate_template_and_exit() -> int
    _infer_dialect_from_drivername(drivername) -> str | None

Internal exception:
    _UserCancelledError  — re-exported so wizard.py can catch it from one place
"""

from __future__ import annotations

from dbread.connstr.types import ParsedConn


class _UserCancelledError(Exception):
    """Raised when the user presses Ctrl-C or sends EOF during a manual prompt."""


# Recognised dialect tokens accepted at the manual-entry prompt
_VALID_DIALECTS = frozenset(
    {"postgres", "mysql", "mssql", "oracle", "sqlite", "duckdb", "clickhouse", "mongodb"}
)

# Map SQLAlchemy drivername base → dbread dialect token
_DRIVERNAME_MAP: dict[str, str] = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mysql": "mysql",
    "mssql": "mssql",
    "sqlserver": "mssql",
    "oracle": "oracle",
    "sqlite": "sqlite",
    "duckdb": "duckdb",
    "clickhouse": "clickhouse",
    "mongodb": "mongodb",
}


def _infer_dialect_from_drivername(drivername: str) -> str | None:
    """Map SQLAlchemy drivername like 'postgresql+psycopg2' → 'postgres'."""
    base = drivername.split("+")[0].lower()
    return _DRIVERNAME_MAP.get(base)


def _manual_url_entry(dialect_hint: str | None) -> ParsedConn:
    """Prompt user for a SQLAlchemy URL and optional dialect.

    Validates URL syntax via ``sqlalchemy.engine.make_url`` before returning.
    Raises _UserCancelledError on Ctrl-C or EOF.
    """
    print()
    print("Enter SQLAlchemy URL (e.g. postgresql+psycopg2://user:pw@host/db).")
    print("See docs/connection-string-formats.md for templates per dialect.")

    while True:
        try:
            url = input("URL: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise _UserCancelledError from exc

        if not url:
            print("URL cannot be empty.")
            continue

        # Validate via sqlalchemy.make_url — raises ArgumentError on bad syntax
        try:
            from sqlalchemy.engine import make_url  # noqa: PLC0415

            sa_url = make_url(url)
        except Exception as exc:  # noqa: BLE001
            print(f"Invalid URL: {exc}. Try again.")
            continue

        # Resolve dialect: hint > inferred from drivername > interactive prompt
        dialect = dialect_hint or _infer_dialect_from_drivername(sa_url.drivername)
        if dialect is None:
            try:
                dialect = input(
                    "Dialect (postgres/mysql/mssql/oracle/sqlite/duckdb/clickhouse/mongodb): "
                ).strip()
            except (EOFError, KeyboardInterrupt) as exc:
                raise _UserCancelledError from exc
            if dialect not in _VALID_DIALECTS:
                print("Unrecognised dialect. Try again.")
                continue

        return ParsedConn(
            format="manual",
            dialect=dialect,  # type: ignore[arg-type]
            host=sa_url.host,
            port=sa_url.port,
            database=sa_url.database,
            user=sa_url.username,
            password=sa_url.password,
            params=dict(sa_url.query),
            raw=url,
        )


def _generate_template_and_exit() -> int:
    """Print copy-paste YAML + .env template so user can edit manually. Returns 1."""
    print()
    print("Copy this into ~/.dbread/config.yaml under `connections:`:")
    print()
    print("  myconn:")
    print("    url_env: MYCONN_URL")
    print("    dialect: postgres   # change to mysql/mssql/oracle/sqlite/duckdb/clickhouse/mongodb")
    print("    rate_limit_per_min: 60")
    print("    statement_timeout_s: 30")
    print("    max_rows: 1000")
    print()
    print("And add the SQLAlchemy URL to ~/.dbread/.env:")
    print("  MYCONN_URL=postgresql+psycopg2://user:pw@host:5432/db")
    print()
    print("Then run: dbread doctor")
    return 1


def _offer_fallback_menu(raw: str, dialect_hint: str | None) -> ParsedConn | int:  # noqa: ARG001
    """Present 3-option recovery menu when auto-detection fails.

    Args:
        raw: the original connection string that could not be detected.
        dialect_hint: optional dialect override passed to manual entry.

    Returns:
        ParsedConn if the user chose manual entry (option 1).
        int exit-code (1) for template-and-exit (option 2) or cancel (option 3).
    """
    print()
    print("Fallback options:")
    print("  1) Enter SQLAlchemy URL manually")
    print("  2) Generate config template + exit (edit ~/.dbread/config.yaml by hand)")
    print("  3) Cancel")

    try:
        choice = input("Choice [1/2/3]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return 1

    if choice == "1":
        return _manual_url_entry(dialect_hint)
    if choice == "2":
        return _generate_template_and_exit()
    # option 3 or anything else → cancel
    return 1
