"""Cloud-specific connection string parser.

Handles:
- mongodb+srv://  (Atlas SRV)
- md: / md:dbname?motherduck_token=...  (MotherDuck / DuckDB cloud)
- *.clickhouse.cloud hostnames
"""

from __future__ import annotations

from urllib.parse import parse_qsl

from dbread.connstr.parsers import uri as uri_parser
from dbread.connstr.types import ParsedConn


def _parse_mongodb_srv(raw: str, dialect_hint: str | None) -> ParsedConn:
    """Parse mongodb+srv:// → uri parse then stamp format=cloud + srv=true."""
    result = uri_parser.parse(raw, dialect_hint=dialect_hint)
    result.format = "cloud"
    result.params["srv"] = "true"
    result.raw = raw
    return result


def _parse_motherduck(raw: str, dialect_hint: str | None) -> ParsedConn:
    """Parse md:dbname or md:dbname?motherduck_token=TOKEN.

    Stores full 'md:dbname' in database field so the converter can produce
    duckdb:///md:dbname?motherduck_token=... directly.
    """
    # Strip leading md:
    body = raw[3:]  # everything after "md:"

    token: str | None = None
    dbname: str

    if "?" in body:
        dbname_part, query = body.split("?", 1)
        qs = dict(parse_qsl(query))
        token = qs.pop("motherduck_token", None)
        params_extra = qs
    else:
        dbname_part = body
        params_extra = {}

    dbname = dbname_part.strip() or ""

    params: dict[str, str] = {}
    if token:
        params["motherduck_token"] = token
    params.update(params_extra)

    return ParsedConn(
        format="cloud",
        dialect="duckdb",
        host=None,
        port=None,
        database=f"md:{dbname}" if dbname else "md:",
        user=None,
        password=None,
        params=params,
        raw=raw,
    )


def from_uri_parsed(parsed: ParsedConn) -> ParsedConn:
    """Upgrade an already-parsed URI result for *.clickhouse.cloud hostnames.

    Sets port=8443 (HTTPS) if not specified and marks params['secure']='true'.
    """
    if parsed.port is None:
        parsed.port = 8443
    parsed.params["secure"] = "true"
    parsed.format = "cloud"
    return parsed


def parse(raw: str, *, dialect_hint: str | None = None) -> ParsedConn:
    """Dispatch to the correct cloud sub-parser."""
    lower = raw.strip().lower()

    if lower.startswith("mongodb+srv://"):
        return _parse_mongodb_srv(raw, dialect_hint)

    if lower.startswith("md:"):
        return _parse_motherduck(raw, dialect_hint)

    # *.clickhouse.cloud — parse as URI first then upgrade
    if "://" in raw:
        result = uri_parser.parse(raw, dialect_hint=dialect_hint)
        if result.host and ".clickhouse.cloud" in result.host.lower():
            return from_uri_parsed(result)
        # Not a cloud hostname after all — return as-is but mark cloud
        result.format = "cloud"
        return result

    # Plain clickhouse.cloud hostname without scheme
    result = ParsedConn(
        format="cloud",
        dialect="clickhouse",
        host=raw.strip(),
        port=8443,
        params={"secure": "true"},
        raw=raw,
    )
    return result
