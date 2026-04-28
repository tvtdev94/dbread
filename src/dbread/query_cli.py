"""``dbread query`` CLI subcommand — terminal-friendly wrapper around ToolHandlers.

Reuses the SAME guard / cost-guard / rate-limit / audit pipeline as the MCP
server path. Output auto-detects: TTY → ASCII table, pipe → JSON Lines.
``--format csv`` produces RFC-4180 quoted CSV. Exit codes: 0 ok, 1 generic,
2 guard reject, 3 rate limit, 4 connection error.

No ``--no-audit`` flag — bypassing the audit log defeats dbread's value
proposition.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

_USAGE = """\
Usage: dbread query <connection> [opts] "<sql>"
       dbread query <connection> [opts] --command '<json>'

Options:
  --explain           Return the query plan instead of running the query
  --command <json>    Mongo command spec (mongodb dialect only)
  --max-rows <int>    Override server max_rows for this call
  --format <fmt>      Force output: table | json | csv (default: auto)
"""


def main(args: list[str]) -> int:
    parsed, code = _parse_args(args)
    if parsed is None:
        return code

    # Lazy imports — avoid SQLAlchemy on `dbread --help` etc.
    from dotenv import load_dotenv

    from .audit import AuditLogger
    from .config import Settings
    from .connections import ConnectionManager
    from .cost_guard import CostGuard
    from .rate_limiter import RateLimiter
    from .sql_guard import SqlGuard
    from .tools import ToolError, ToolHandlers

    cfg_path = os.environ.get("DBREAD_CONFIG", "config.yaml")
    env_path = Path(cfg_path).resolve().parent / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)

    try:
        settings = Settings.load(cfg_path)
    except FileNotFoundError:
        print(f"config not found: {cfg_path}", file=sys.stderr)
        return 4

    cm = ConnectionManager(settings)
    audit = AuditLogger(
        settings.audit.path, settings.audit.rotate_mb,
        timezone=settings.audit.timezone,
        redact_literals=settings.audit.redact_literals,
        retention_days=settings.audit.retention_days,
    )
    mongo_mgr, mongo_handlers = _maybe_build_mongo(
        settings, cm, RateLimiter(settings), audit,
    )
    handlers = ToolHandlers(
        settings=settings, conn_mgr=cm, guard=SqlGuard(),
        rate_limiter=RateLimiter(settings), audit=audit,
        cost_guard=CostGuard(), mongo=mongo_handlers,
    )

    try:
        if parsed["explain"]:
            result = handlers.explain(
                parsed["conn"], sql=parsed["sql"], command=parsed["cmd"],
            )
        else:
            result = handlers.query(
                parsed["conn"], sql=parsed["sql"], command=parsed["cmd"],
                max_rows=parsed["max_rows"],
            )
    except ToolError as e:
        return _handle_tool_error(str(e))
    except Exception as e:  # noqa: BLE001 — last-line error reporting
        print(f"error: {e}", file=sys.stderr)
        return 4
    finally:
        cm.close_all()
        if mongo_mgr is not None:
            mongo_mgr.close_all()

    fmt = parsed["fmt"] or ("table" if sys.stdout.isatty() else "json")
    if parsed["explain"]:
        print(json.dumps(result.get("plan"), default=str, ensure_ascii=False, indent=2))
        return 0
    _render(result, fmt)
    return 0


def _parse_args(args: list[str]) -> tuple[dict | None, int]:
    """Return (parsed-dict, 0) or (None, exit_code)."""
    conn: str | None = None
    sql: str | None = None
    cmd: dict | None = None
    explain = False
    max_rows: int | None = None
    fmt: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(_USAGE)
            return None, 0
        if a == "--explain":
            explain = True
        elif a == "--command":
            i += 1
            if i >= len(args):
                return _err_parse("--command requires JSON arg")
            try:
                cmd = json.loads(args[i])
            except json.JSONDecodeError as e:
                return _err_parse(f"invalid JSON for --command: {e}")
        elif a == "--max-rows":
            i += 1
            if i >= len(args):
                return _err_parse("--max-rows requires int")
            try:
                max_rows = int(args[i])
            except ValueError:
                return _err_parse(f"--max-rows must be int, got {args[i]!r}")
        elif a == "--format":
            i += 1
            if i >= len(args) or args[i] not in ("table", "json", "csv"):
                return _err_parse("--format must be one of: table, json, csv")
            fmt = args[i]
        elif a.startswith("--"):
            return _err_parse(f"unknown flag: {a}")
        elif conn is None:
            conn = a
        elif sql is None:
            sql = a
        else:
            return _err_parse(f"unexpected positional: {a}")
        i += 1
    if conn is None:
        return _err_parse("connection name required")
    if sql is None and cmd is None:
        return _err_parse("either SQL string or --command required")
    return (
        {"conn": conn, "sql": sql, "cmd": cmd, "explain": explain,
         "max_rows": max_rows, "fmt": fmt},
        0,
    )


def _err_parse(msg: str) -> tuple[None, int]:
    print(f"error: {msg}\n\n{_USAGE}", file=sys.stderr)
    return None, 2


def _handle_tool_error(msg: str) -> int:
    print(msg, file=sys.stderr)
    if any(p in msg for p in ("sql_guard:", "mongo_guard:", "cost_guard_error")):
        return 2
    if "rate_limit_exceeded" in msg:
        return 3
    return 4


def _maybe_build_mongo(settings, cm, rate_limiter, audit):  # noqa: ANN001, ANN201
    """Return (mongo_mgr, mongo_handlers) so caller can close the manager."""
    if not any(c.dialect == "mongodb" for c in settings.connections.values()):
        return None, None
    from .mongo.client import MongoClientManager
    from .mongo.cost_guard import MongoCostGuard
    from .mongo.tools import MongoToolHandlers

    mongo_mgr = MongoClientManager(settings)
    handlers = MongoToolHandlers(
        conn_mgr=cm, mongo_mgr=mongo_mgr, rate_limiter=rate_limiter,
        audit=audit, cost_guard=MongoCostGuard(),
    )
    return mongo_mgr, handlers


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(result: dict, fmt: str) -> None:
    columns = result.get("columns", [])
    rows = result.get("rows", [])
    if fmt == "json":
        for r in rows:
            print(json.dumps(dict(zip(columns, r, strict=False)), default=str, ensure_ascii=False))
        return
    if fmt == "csv":
        w = csv.writer(sys.stdout, lineterminator="\n")
        w.writerow(columns)
        for r in rows:
            w.writerow(["" if v is None else str(v) for v in r])
        return
    _render_table(columns, rows)


def _render_table(columns: list[str], rows: list[list[Any]]) -> None:
    if not columns:
        print("(no columns)")
        return
    cell = lambda v: "" if v is None else str(v)  # noqa: E731
    truncated = [
        [(cell(v)[:47] + "…") if len(cell(v)) > 50 else cell(v) for v in row]
        for row in rows
    ]
    widths = [len(c) for c in columns]
    for r in truncated:
        for j, v in enumerate(r):
            if j < len(widths):
                widths[j] = max(widths[j], len(v))
    sep = "─"
    print(" │ ".join(c.ljust(widths[j]) for j, c in enumerate(columns)))
    print("─┼─".join(sep * w for w in widths))
    for r in truncated:
        cells = [(r[j] if j < len(r) else "").ljust(widths[j]) for j in range(len(columns))]
        print(" │ ".join(cells))
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
