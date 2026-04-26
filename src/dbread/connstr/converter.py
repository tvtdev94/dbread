"""Convert ParsedConn to a SQLAlchemy-compatible URL string.

Public API:
    to_sqlalchemy_url(parsed: ParsedConn) -> str
    DRIVER_SUFFIX  -- dialect -> drivername mapping
    DEFAULT_PORT   -- dialect -> default port (int) mapping
"""

from __future__ import annotations

import urllib.parse

from sqlalchemy.engine import URL

from dbread.connstr.types import ParsedConn

# ---------------------------------------------------------------------------
# Dialect tables
# ---------------------------------------------------------------------------

DRIVER_SUFFIX: dict[str, str] = {
    "postgres": "postgresql+psycopg2",
    "mysql": "mysql+pymysql",
    "mssql": "mssql+pyodbc",
    "oracle": "oracle+oracledb",
    "sqlite": "sqlite",
    "duckdb": "duckdb",
    "clickhouse": "clickhouse+http",
    "mongodb": "mongodb",
}

DEFAULT_PORT: dict[str, int] = {
    "postgres": 5432,
    "mysql": 3306,
    "mssql": 1433,
    "oracle": 1521,
    "clickhouse": 8123,
    "mongodb": 27017,
}

# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------


def to_sqlalchemy_url(p: ParsedConn) -> str:
    """Return a canonical SQLAlchemy URL string for the given ParsedConn.

    Handles all supported dialects. Uses sqlalchemy.URL.create() for SQL
    dialects (automatic password escaping). Builds MongoDB and file-based
    URLs manually.

    Args:
        p: Normalised connection data produced by any parser.

    Returns:
        A SQLAlchemy-compatible URL string.

    Raises:
        KeyError: if dialect is not in DRIVER_SUFFIX.
    """
    d = p.dialect

    if d in {"sqlite", "duckdb"}:
        return _build_filepath_url(p)

    if d == "mongodb":
        return _build_mongo_url(p)

    # --- SQL-family dialects: let SQLAlchemy handle escaping ---
    query_params = dict(p.params)

    # ClickHouse Cloud: secure flag → default port 8443
    if d == "clickhouse" and query_params.get("secure") == "true":
        port = p.port or 8443
    else:
        port = p.port or DEFAULT_PORT.get(d)

    url = URL.create(
        drivername=DRIVER_SUFFIX[d],
        username=p.user,
        password=p.password,
        host=p.host,
        port=port,
        database=p.database,
        query=query_params,  # type: ignore[arg-type]  # accepts Mapping[str,str]
    )
    # render_as_string(hide_password=False) is required — str(url) replaces
    # the password with "***" which makes the output unusable as a real URL.
    return url.render_as_string(hide_password=False)


# ---------------------------------------------------------------------------
# File-based dialects: SQLite / DuckDB
# ---------------------------------------------------------------------------


def _build_filepath_url(p: ParsedConn) -> str:
    """Build a sqlite:/// or duckdb:/// URL from a file-path ParsedConn.

    Slash rules:
        :memory:              -> sqlite:///:memory:
        relative path         -> sqlite:///rel/path.db    (3 slashes)
        Unix absolute         -> sqlite:////abs/path.db   (4 slashes: 3 + leading /)
        Windows absolute      -> sqlite:///C:/path/db     (3 slashes + drive letter)
        MotherDuck (md:...)   -> duckdb:///md:dbname      (with query params appended)
    """
    prefix = DRIVER_SUFFIX[p.dialect]  # "sqlite" or "duckdb"
    db = p.database or ""

    # :memory: shortcut
    if db == ":memory:":
        return f"{prefix}:///:memory:"

    # MotherDuck cloud databases (duckdb only, database starts with "md:")
    if p.dialect == "duckdb" and db.startswith("md:"):
        query = _encode_query_params(p.params)
        return f"{prefix}:///{db}{query}"

    # Normalise OS path: replace backslashes with forward slashes
    db = db.replace("\\", "/")

    # Windows absolute path: starts with drive letter, e.g. "C:/..."
    if len(db) >= 2 and db[1] == ":":
        # 3 slashes total; drive letter is the 4th character
        return f"{prefix}:///{db}"

    # Unix absolute path: starts with "/"
    if db.startswith("/"):
        # 4 slashes total (3 from ":///", 1 from leading "/")
        return f"{prefix}:///{db}"

    # Relative path
    return f"{prefix}:///{db}"


# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------


def _build_mongo_url(p: ParsedConn) -> str:
    """Build a mongodb:// or mongodb+srv:// URL without using URL.create().

    URL.create does not cleanly support the +srv scheme variant, so we
    hand-construct the URL and percent-encode credentials ourselves.

    The 'srv' key in p.params is metadata (not a real query param) and is
    intentionally omitted from the output query string.
    """
    params = dict(p.params)
    is_srv = params.pop("srv", None) == "true"
    scheme = "mongodb+srv" if is_srv else "mongodb"

    # Percent-encode credentials — safe='' ensures all specials are escaped
    user_enc = urllib.parse.quote(p.user, safe="") if p.user else None
    pwd_enc = urllib.parse.quote(p.password, safe="") if p.password else None

    # Auth prefix: user:pass@ / user@ / (empty)
    if user_enc and pwd_enc:
        auth = f"{user_enc}:{pwd_enc}@"
    elif user_enc:
        auth = f"{user_enc}@"
    else:
        auth = ""

    # Host + optional port (SRV records must NOT include a port)
    if is_srv:
        host_part = p.host or ""
    else:
        port = p.port or DEFAULT_PORT.get("mongodb")
        host_part = f"{p.host}:{port}" if p.host and port else (p.host or "")

    db_part = f"/{p.database}" if p.database else ""

    query = _encode_query_params(params)

    return f"{scheme}://{auth}{host_part}{db_part}{query}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _encode_query_params(params: dict[str, str]) -> str:
    """Return '?key=val&...' from a params dict, or '' if empty."""
    if not params:
        return ""
    return "?" + urllib.parse.urlencode(params)
