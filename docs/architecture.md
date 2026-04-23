# Architecture

## Overview

dbread is a single-process Python MCP server that proxies **read-only** SQL queries from an AI client (Claude Code) to one or more databases. It stands between the AI and the DB to enforce defense in depth.

## Component Diagram

```
┌──────────────┐    stdio    ┌─────────────────────────────────────────────┐
│ Claude Code  │◄───────────►│            dbread MCP Server                │
│ (MCP client) │   (JSON)    │                                             │
└──────────────┘             │   ┌──────────┐                              │
                             │   │  tools   │  list_connections            │
                             │   │          │  list_tables                 │
                             │   │          │  describe_table              │
                             │   │          │  query                       │
                             │   │          │  explain                     │
                             │   └────┬─────┘                              │
                             │        │                                    │
                             │   ┌────▼────┐  ┌────────┐  ┌────────┐       │
                             │   │sql_guard│  │rate_lmt│  │ audit  │       │
                             │   └────┬────┘  └───┬────┘  └───┬────┘       │
                             │        │           │           │            │
                             │        └─────┬─────┴─────┬─────┘            │
                             │              │           │                  │
                             │         ┌────▼────┐ ┌────▼─────┐            │
                             │         │ config  │ │connection│            │
                             │         │ YAML    │ │ manager  │            │
                             │         └─────────┘ └────┬─────┘            │
                             │                          │                  │
                             └──────────────────────────┼──────────────────┘
                                                        │
                                       ┌────────────────┼─────────────────┐
                                       ▼                ▼                 ▼
                                  ┌─────────┐      ┌────────┐       ┌─────────┐
                                  │Postgres │      │ MySQL  │  ...  │ SQLite  │
                                  │(RO user)│      │(RO user)│      │(ro mode)│
                                  └─────────┘      └────────┘       └─────────┘
```

## Supported Dialects

| `dialect` | DB engine | Extras (optional) | Layer-0 approach |
|-----------|-----------|-------------------|------------------|
| `postgres` | PostgreSQL 12+ (incl. Cockroach, Timescale, Aurora PG, Yugabyte) | `dbread[postgres]` | read-only DB user + `default_transaction_read_only` |
| `mysql` | MySQL 8+ (incl. Aurora MySQL, SingleStore, PlanetScale) | `dbread[mysql]` | read-only DB user + `GRANT SELECT` |
| `mssql` | SQL Server 2019+ | `dbread[mssql]` | `db_datareader` role + `DENY EXECUTE` |
| `sqlite` | SQLite 3 | (built-in) | `mode=ro&uri=true` URL + file perms |
| `oracle` | Oracle 19c+ | `dbread[oracle]` | per-table `GRANT SELECT` + resource profile |
| `duckdb` | DuckDB 1.x | `dbread[duckdb]` | `access_mode=read_only` URL + file perms |
| `clickhouse` | ClickHouse 24+ | `dbread[clickhouse]` | `readonly` profile + connect-arg `readonly=1` |

## 5-Layer Defense in Depth

| Layer | Mechanism | Rejects |
|-------|-----------|---------|
| 0 | DB user with `GRANT SELECT` only | Any write; privileged ops |
| 1 | sqlglot AST validation | DML / DDL / DCL, multi-statement, CTE-DML, side-effect functions |
| 2 | Rate limiter + DB `statement_timeout` | Runaway loops; long queries |
| 3 | Auto-inject `LIMIT N` | Oversized result sets |
| 4 | Audit JSONL log | *(detection, not prevention)* |

**Principle:** never rely on a single layer. Layer 0 is the non-bypassable guarantee.

## Data Flow — `query` Tool

```
1. MCP client calls: query(connection="x", sql="SELECT ...")

2. tools.query():
   a. guard.validate(sql, dialect)       # Layer 1 — may reject
   b. sql = guard.inject_limit(sql)      # Layer 3
   c. rate_limiter.acquire(connection)   # Layer 2a — may reject
   d. engine.execute(sql)                # hits Layer 2b (DB-side timeout) + Layer 0 (RO user)
   e. audit.log(...)                     # Layer 4

3. Return rows JSON to MCP client.
```

## File Layout

```
src/dbread/
├── __init__.py          # version
├── audit.py             # JSONL append + rotation
├── config.py            # pydantic Settings (YAML + env)
├── connections.py       # SQLAlchemy engine manager
├── rate_limiter.py      # token bucket per connection
├── server.py            # MCP server entry (stdio)
├── sql_guard.py         # sqlglot AST validation + LIMIT injection
└── tools.py             # 5 tool handlers wiring everything
```

## Design Decisions

- **SQLAlchemy 2.x** — multi-dialect inspector; no hand-rolled metadata queries.
- **sqlglot** — 20+ SQL dialects AST; handles CTE-DML edge cases.
- **JSONL audit** — append-only, `grep`/`jq` friendly, resilient to crashes.
- **stdio transport** — matches Claude Code's native MCP config.
- **In-memory rate limit** — single-process scope; simpler than Redis (YAGNI).
- **Two-level rate limit** (v0.3) — optional `global_rate_limit_per_min` caps
  total QPM across all connections; AND-ed with per-connection bucket.
  Defends against rotation attacks (prompt-injection cycling connection
  names to multiply effective throughput). Unset by default.

## Non-Goals

- Multi-tenancy.
- Persistent rate limit (a restart resets all buckets).
- NoSQL support (Mongo / Redis) — future extension if needed.
- Network RPC — stdio only.
