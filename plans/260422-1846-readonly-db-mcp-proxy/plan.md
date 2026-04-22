---
title: "Read-only DB MCP Proxy for AI"
description: "Python MCP server giới hạn Claude Code truy cập multi-DB ở read-only mode, defense in depth 5 layers"
status: completed
priority: P1
effort: 20h
completed: 2026-04-22
branch: main
tags: [mcp, security, python, database, read-only]
created: 2026-04-22
slug: readonly-db-mcp-proxy
---

# Plan: Read-only DB MCP Proxy

## Problem

Đưa connection string trực tiếp cho AI → rủi ro prompt-injection chạy `UPDATE/DELETE/DROP`, runaway query đốt DB, thiếu audit để forensic. Cần 1 MCP server unified đa DB (PG/MySQL/MSSQL/SQLite/Oracle), chỉ cho đọc, có rate limit + audit.

## Solution

Python 3.11+ MCP server dùng `sqlglot` AST validation + SQLAlchemy 2.x multi-DB + token bucket rate limiter + JSONL audit. Defense in depth 5 lớp: (0) DB user GRANT SELECT — non-negotiable, (1) sqlglot reject DML/DDL/DCL, (2) rate limit + statement_timeout, (3) auto-inject LIMIT 1000, (4) audit mọi query. File < 200 LOC, `uv` package manager, config YAML + env.

## Phases

| # | Phase | Status | Effort | File |
|---|-------|--------|--------|------|
| 01 | Setup & Scaffolding | completed | 1h | [phase-01-setup-scaffolding.md](phase-01-setup-scaffolding.md) |
| 02 | Core Foundation (config + connections + audit) | completed | 3h | [phase-02-core-foundation.md](phase-02-core-foundation.md) |
| 03 | SQL Guard (CRITICAL) | completed | 5h | [phase-03-sql-guard.md](phase-03-sql-guard.md) |
| 04 | Rate Limiter | completed | 2h | [phase-04-rate-limiter.md](phase-04-rate-limiter.md) |
| 05 | MCP Tools Wiring | completed | 3h | [phase-05-mcp-tools.md](phase-05-mcp-tools.md) |
| 06 | Documentation | completed | 3h | [phase-06-documentation.md](phase-06-documentation.md) |
| 07 | Integration Tests | completed | 3h | [phase-07-integration-tests.md](phase-07-integration-tests.md) |

## Results

- **93 tests pass, 2 skipped** (PG/MySQL integration — skip when driver not installed, graceful)
- **80% overall coverage** (server.py 0% — manual smoke only; all business logic > 89%)
- **All source files < 200 LOC** (max: tools.py 156, server.py 154)
- **5 MCP tools working**: list_connections, list_tables, describe_table, query, explain
- **SQL Guard coverage**: 48 test cases (35 validation + 13 LIMIT/AST) at 91% coverage — CTE-DML, function blacklist, multi-statement, comment/case evasion all blocked
- **Docs shipped**: setup-db-readonly.md (5 DBs), architecture.md, security-threat-model.md, manual-smoke-test.md, expanded README

## Dependencies

**External:**
- `mcp` (Anthropic official SDK)
- `sqlglot` (multi-dialect AST parser)
- `sqlalchemy` 2.x (DB abstraction + inspector)
- `pydantic-settings` (typed config)
- Drivers: `psycopg2-binary`, `pymysql`, `pyodbc`, `oracledb` (optional extras)
- `uv` package manager

**Internal:**
- Phase 02 blocks 03, 04, 05
- Phase 03 + 04 block 05
- Phase 05 blocks 07
- Phase 06 parallel với 03-05

## Links

- Brainstorm report: [../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md](../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md)
- Docs output: `./docs/setup-db-readonly.md`, `./docs/architecture.md`, `./docs/security-threat-model.md`
