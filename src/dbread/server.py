"""MCP server entry point - stdio transport, registers 5 tools."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .audit import AuditLogger
from .config import Settings
from .connections import ConnectionManager
from .rate_limiter import RateLimiter
from .sql_guard import SqlGuard
from .tools import ToolError, ToolHandlers

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
log = logging.getLogger("dbread")

SERVER_NAME = "dbread"
SERVER_VERSION = "0.5.0"


def _tool_schemas() -> list[Tool]:
    return [
        Tool(
            name="list_connections",
            description="List all configured database connections with their dialects.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="list_tables",
            description="List tables in a configured database connection.",
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {"type": "string", "description": "Connection name"},
                    "schema": {"type": "string", "description": "Optional schema filter"},
                },
                "required": ["connection"],
            },
        ),
        Tool(
            name="describe_table",
            description="Describe columns (name, type, nullable, pk) and indexes of a table.",
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {"type": "string"},
                    "table": {"type": "string"},
                    "schema": {"type": "string"},
                },
                "required": ["connection", "table"],
            },
        ),
        Tool(
            name="query",
            description=(
                "Run a read-only query. Provide exactly one of `sql` (SQL "
                "dialects: SELECT/WITH) or `command` (mongodb dialect: JSON "
                "command spec — find/count/distinct/aggregate). Results "
                "auto-limited, rate-limited, audited. Server rejects the call "
                "with invalid_input if neither or both are set for the target "
                "dialect."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {"type": "string"},
                    "sql": {"type": "string", "description": "SQL SELECT — for SQL dialects"},
                    "command": {
                        "type": "object",
                        "description": "MongoDB command spec — for mongodb dialect",
                    },
                    "max_rows": {"type": "integer", "minimum": 1},
                },
                "required": ["connection"],
            },
        ),
        Tool(
            name="explain",
            description=(
                "Return the query execution plan. SQL connections: pass `sql`. "
                "MongoDB connections: pass `command`. Server rejects with "
                "invalid_input if the wrong field is provided for the dialect."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "connection": {"type": "string"},
                    "sql": {"type": "string"},
                    "command": {"type": "object"},
                },
                "required": ["connection"],
            },
        ),
    ]


async def _run() -> None:
    config_path = os.environ.get("DBREAD_CONFIG", "config.yaml")
    # Auto-load sibling .env so url_env references resolve without leaking
    # credentials into the MCP config.
    env_path = Path(config_path).resolve().parent / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=False)
    settings = Settings.load(config_path)

    cm = ConnectionManager(settings)
    rate_limiter = RateLimiter(settings)
    audit = AuditLogger(
        settings.audit.path,
        settings.audit.rotate_mb,
        timezone=settings.audit.timezone,
        redact_literals=settings.audit.redact_literals,
        retention_days=settings.audit.retention_days,
    )

    has_mongo = any(c.dialect == "mongodb" for c in settings.connections.values())
    mongo_mgr = None
    mongo_handlers = None
    if has_mongo:
        from .mongo.client import MongoClientManager
        from .mongo.tools import MongoToolHandlers

        mongo_mgr = MongoClientManager(settings)
        mongo_handlers = MongoToolHandlers(
            conn_mgr=cm,
            mongo_mgr=mongo_mgr,
            rate_limiter=rate_limiter,
            audit=audit,
        )

    handlers = ToolHandlers(
        settings=settings,
        conn_mgr=cm,
        guard=SqlGuard(),
        rate_limiter=rate_limiter,
        audit=audit,
        mongo=mongo_handlers,
    )

    server: Server = Server(SERVER_NAME)
    schemas = _tool_schemas()

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return schemas

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict | None) -> list[TextContent]:
        args = arguments or {}
        try:
            fn = getattr(handlers, name, None)
            if fn is None:
                payload = {"error": f"unknown_tool: {name}"}
            else:
                # Offload sync DB / SQLAlchemy I/O to the default thread pool so
                # a slow query never blocks the MCP event loop.
                payload = await asyncio.to_thread(fn, **args)
        except ToolError as e:
            payload = {"error": str(e)}
        except TypeError as e:
            payload = {"error": f"invalid_arguments: {e}"}
        except Exception as e:
            log.exception("tool error")
            payload = {"error": f"internal: {type(e).__name__}"}
        return [TextContent(type="text", text=json.dumps(payload, default=str, ensure_ascii=False))]

    try:
        async with stdio_server() as (reader, writer):
            await server.run(
                reader,
                writer,
                InitializationOptions(
                    server_name=SERVER_NAME,
                    server_version=SERVER_VERSION,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        cm.close_all()
        if mongo_mgr is not None:
            mongo_mgr.close_all()


def main() -> None:
    args = sys.argv[1:]
    if args:
        if args[0] in {"-h", "--help", "help"}:
            from .cli import print_help
            sys.exit(print_help())
        if args[0] in {"-V", "--version", "version"}:
            print(f"dbread {SERVER_VERSION}")
            return
        if args[0] == "init":
            from .cli import init_config
            sys.exit(init_config())
        if args[0] == "audit":
            from .audit_cli import main as audit_main
            sys.exit(audit_main(args[1:]))
        print(f"unknown argument: {args[0]}. Try `dbread --help`.", file=sys.stderr)
        sys.exit(2)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
