"""Shared ADO.NET / ODBC tokenizer and block-checks (internal module)."""

from __future__ import annotations

from dbread.connstr.types import UnsupportedConnString


def tokenize(raw: str) -> dict[str, str]:
    """Split an ADO.NET-style string into {key: value} pairs.

    Handles quoted values that contain semicolons, e.g. Password="a;b".
    Keys are returned lower-cased and stripped; values are stripped.
    """
    result: dict[str, str] = {}
    i = 0
    n = len(raw)

    while i < n:
        # Skip leading whitespace / trailing semicolons
        while i < n and raw[i] in " \t\r\n;":
            i += 1
        if i >= n:
            break

        # Collect key up to '='
        key_start = i
        while i < n and raw[i] != "=" and raw[i] != ";":
            i += 1
        key = raw[key_start:i].strip().lower()
        if not key or i >= n or raw[i] != "=":
            # No '=' found — skip token
            while i < n and raw[i] != ";":
                i += 1
            continue
        i += 1  # skip '='

        # Skip optional whitespace before value
        while i < n and raw[i] in " \t":
            i += 1

        # Collect value — handle quoted strings
        value: str
        if i < n and raw[i] in ('"', "'"):
            quote_char = raw[i]
            i += 1
            val_start = i
            while i < n and raw[i] != quote_char:
                i += 1
            value = raw[val_start:i]
            if i < n:
                i += 1  # skip closing quote
            # Skip to next ';'
            while i < n and raw[i] != ";":
                i += 1
        else:
            val_start = i
            while i < n and raw[i] != ";":
                i += 1
            value = raw[val_start:i].strip()

        if key:
            result[key] = value

    return result


def check_blocked(tokens: dict[str, str]) -> None:
    """Raise UnsupportedConnString for Windows-auth and named-instance patterns."""
    tc = tokens.get("trusted_connection", tokens.get("trusted connection", "")).lower()
    if tc in ("yes", "true", "sspi"):
        raise UnsupportedConnString(
            "Windows authentication (Trusted_Connection) detected",
            hint=(
                "Windows authentication is not supported. "
                "Provide explicit user/password credentials or set up Kerberos in your URL."
            ),
        )

    is_sec = tokens.get("integrated security", tokens.get("integratedsecurity", "")).lower()
    if is_sec in ("sspi", "true", "yes"):
        raise UnsupportedConnString(
            "Integrated Security detected",
            hint=(
                "Windows authentication is not supported. "
                "Provide explicit user/password credentials or set up Kerberos in your URL."
            ),
        )

    # Named instance: Server=HOST\SQLEXPRESS
    server_val = tokens.get("server") or tokens.get("data source") or tokens.get("host") or ""
    if server_val.lower().startswith("tcp:"):
        server_check = server_val.lstrip("tcp:").strip()
    else:
        server_check = server_val
    if "\\" in server_check:
        raise UnsupportedConnString(
            f"Named SQL Server instance detected: {server_val!r}",
            hint=(
                "Named instances require an ODBC DSN. "
                "Use IP:port instead (e.g. Server=192.168.1.1,1433) "
                "or pre-configure an ODBC DSN."
            ),
        )


def extract_host_port(raw_server: str) -> tuple[str, int | None]:
    """Parse 'tcp:host,1433', 'host,1433', 'host:1433', or plain host."""
    value = raw_server.strip()
    # Strip tcp: prefix (Azure SQL)
    if value.lower().startswith("tcp:"):
        value = value[4:].strip()

    # Comma-separated port (MSSQL style: host,1433)
    if "," in value:
        host_part, port_part = value.rsplit(",", 1)
        port_part = port_part.strip()
        if port_part.isdigit():
            return host_part.strip(), int(port_part)
        return value, None

    # Colon-separated port — but NOT IPv6 (skip if multiple colons)
    if ":" in value and value.count(":") == 1:
        host_part, port_part = value.rsplit(":", 1)
        if port_part.strip().isdigit():
            return host_part.strip(), int(port_part.strip())

    return value, None
