---
phase: 06
title: "Documentation — setup + architecture + threat model"
status: pending
priority: P1
effort: 3h
created: 2026-04-22
---

# Phase 06 — Documentation

## Context Links

- Brainstorm §5 DB Read-Only Setup, §4.2 Architecture, §6 Risks: [../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md](../reports/brainstorm-260422-1846-readonly-db-mcp-proxy.md)
- Previous: [phase-05-mcp-tools.md](phase-05-mcp-tools.md)

## Overview

- **Priority:** P1 (Layer 0 setup mandatory — docs IS the product for that layer)
- **Status:** pending
- **Description:** Write 3 docs (`setup-db-readonly.md`, `architecture.md`, `security-threat-model.md`) + full README quickstart. DB setup docs MUST have copy-paste SQL snippets for PG/MySQL/MSSQL/Oracle/SQLite.

## Key Insights

- **Layer 0 docs = actual security guarantee.** If user fails to setup read-only DB user, everything else is bypassable. So docs must be actionable, not prose.
- Each DB section follows same template: CREATE USER → GRANT minimum → timeout/read-only setting → verify query
- Claude Code MCP config snippet cần JSON format chính xác — user copy-paste vào `claude_desktop_config.json` hoặc `.claude/mcp_servers.json`
- STRIDE threat model (Spoofing, Tampering, Repudiation, Information disclosure, DoS, Elevation of privilege) → table mapping each threat to mitigation layer + residual risk
- Architecture doc dùng ASCII diagram (simple, version-control friendly); Mermaid nếu muốn GitHub render nice (optional)

## Requirements

**Functional:**
- `docs/setup-db-readonly.md` cover 5 DBs (PG, MySQL, MSSQL, Oracle, SQLite) với actionable SQL
- `docs/architecture.md` diagram + explain 5-layer defense + data flow
- `docs/security-threat-model.md` STRIDE table
- Updated `README.md` với install, config, Claude Code MCP config snippet, example tool calls

**Non-functional:**
- User setup < 10 phút theo docs
- Code blocks all tested (copy-paste run được trên real DB)
- Docs cross-linked

## Architecture (doc structure)

```
README.md                         # entry point, 5min quickstart
├── → docs/setup-db-readonly.md   # Layer 0 DB user setup (LONGEST, most detailed)
├── → docs/architecture.md        # overall design
└── → docs/security-threat-model.md  # STRIDE + residual
```

## Related Code Files

**Create:**
- `docs/setup-db-readonly.md`
- `docs/architecture.md`
- `docs/security-threat-model.md`

**Modify:**
- `README.md` — expand quickstart (was stub in Phase 01)

**Delete:** none

## Implementation Steps

### Step 1 — `docs/setup-db-readonly.md`

Structure:

```markdown
# DB Read-Only Setup (Layer 0)

> Non-negotiable security foundation. Parser guard + rate limit are belts; DB user is the suspenders.

## Why This Is Mandatory
- Layer 1 (sqlglot guard) can have bugs. Layer 0 (DB user with no write permission) is the last line that cannot be bypassed by SQL tricks.
- Configure this BEFORE exposing connection to dbread.

## PostgreSQL

### 1. Create user
\`\`\`sql
CREATE USER ai_readonly WITH PASSWORD 'CHANGEME_strong_password';
\`\`\`

### 2. Grant read-only on database + schema
\`\`\`sql
-- Connect to target DB first: \c mydb
GRANT CONNECT ON DATABASE mydb TO ai_readonly;
GRANT USAGE ON SCHEMA public TO ai_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ai_readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO ai_readonly;
-- Future tables auto-grant
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ai_readonly;
\`\`\`

### 3. Enforce read-only transaction + timeout
\`\`\`sql
ALTER USER ai_readonly SET default_transaction_read_only = on;
ALTER USER ai_readonly SET statement_timeout = '30s';
ALTER USER ai_readonly SET idle_in_transaction_session_timeout = '60s';
\`\`\`

### 4. Verify (as ai_readonly)
\`\`\`sql
SELECT 1;                               -- should succeed
CREATE TABLE test(id int);               -- should FAIL: permission denied
UPDATE any_table SET x = 1;              -- should FAIL: read-only transaction
\`\`\`

### 5. Connection string
\`\`\`
postgresql+psycopg2://ai_readonly:strong_password@host:5432/mydb
\`\`\`

## MySQL 8+

### 1. Create user
\`\`\`sql
CREATE USER 'ai_readonly'@'%' IDENTIFIED BY 'CHANGEME_strong_password';
\`\`\`

### 2. Grant
\`\`\`sql
GRANT SELECT, SHOW VIEW ON mydb.* TO 'ai_readonly'@'%';
FLUSH PRIVILEGES;
\`\`\`

### 3. Timeout (per-session hint; global alternative)
\`\`\`sql
-- Global (requires SUPER):
SET GLOBAL max_execution_time = 30000;   -- milliseconds
-- dbread applies init_command per connection; both OK
\`\`\`

### 4. Verify (as ai_readonly)
\`\`\`sql
SELECT 1;                 -- OK
CREATE TABLE t(id int);   -- FAIL
UPDATE any_table ...      -- FAIL
\`\`\`

### 5. Connection string
\`\`\`
mysql+pymysql://ai_readonly:strong_password@host:3306/mydb
\`\`\`

## Microsoft SQL Server

### 1. Create login + user
\`\`\`sql
CREATE LOGIN ai_readonly WITH PASSWORD = 'CHANGEME_Strong_Pw!';
USE mydb;
CREATE USER ai_readonly FOR LOGIN ai_readonly;
\`\`\`

### 2. Grant read-only role + deny execute
\`\`\`sql
ALTER ROLE db_datareader ADD MEMBER ai_readonly;
DENY EXECUTE TO ai_readonly;     -- block stored procs (incl xp_cmdshell)
DENY ALTER, INSERT, UPDATE, DELETE TO ai_readonly;
\`\`\`

### 3. Query timeout (application-level; no session-wide setting)
- dbread passes `timeout` connect_arg to pyodbc. Also: configure `Query Governor Cost Limit` server-wide:
\`\`\`sql
sp_configure 'query governor cost limit', 30;    -- seconds
RECONFIGURE;
\`\`\`

### 4. Verify
\`\`\`sql
SELECT 1;                           -- OK
UPDATE dbo.any_table SET x = 1;     -- FAIL
EXEC xp_cmdshell 'dir';             -- FAIL (DENY EXECUTE)
\`\`\`

### 5. Connection string (ODBC Driver 18)
\`\`\`
mssql+pyodbc://ai_readonly:Strong_Pw!@host/mydb?driver=ODBC+Driver+18+for+SQL+Server
\`\`\`

## Oracle

### 1. Create user + grant
\`\`\`sql
CREATE USER ai_readonly IDENTIFIED BY "CHANGEME_strong_pw";
GRANT CREATE SESSION TO ai_readonly;
GRANT SELECT ANY TABLE TO ai_readonly;    -- or per-table GRANT SELECT ON ... for tighter
-- Do NOT grant CREATE/ALTER/DROP privileges
\`\`\`

### 2. Resource profile (timeout/idle)
\`\`\`sql
CREATE PROFILE readonly_profile LIMIT
  IDLE_TIME 5            -- minutes
  CONNECT_TIME 60
  CPU_PER_CALL 3000;     -- centiseconds = 30s
ALTER USER ai_readonly PROFILE readonly_profile;
\`\`\`

### 3. Verify
\`\`\`sql
SELECT 1 FROM DUAL;          -- OK
CREATE TABLE t(x NUMBER);    -- FAIL: insufficient privileges
\`\`\`

### 4. Connection string
\`\`\`
oracle+oracledb://ai_readonly:strong_pw@host:1521/?service_name=mydb
\`\`\`

## SQLite

SQLite has no user system — enforce via file permissions + read-only open mode.

### 1. File permission (Unix)
\`\`\`bash
chmod 644 mydb.sqlite     # or 444 for strict read-only at FS level
\`\`\`

### 2. Connection string with URI mode
\`\`\`
sqlite:///file:mydb.sqlite?mode=ro&uri=true
\`\`\`

Verification: `UPDATE` should fail with "attempt to write a readonly database".

## Verification Checklist (all DBs)
- [ ] Read queries work
- [ ] `CREATE/ALTER/DROP` fail with permission error
- [ ] `INSERT/UPDATE/DELETE` fail
- [ ] Long query (`SELECT pg_sleep(60)` etc) times out at configured threshold
- [ ] Side-effect functions (where applicable): pg_read_file / xp_cmdshell → permission denied
```

### Step 2 — `docs/architecture.md`

```markdown
# Architecture

## Overview
dbread is a single-process Python MCP server that proxies read-only SQL queries from an AI client (Claude Code) to one or more databases.

## Component Diagram

\`\`\`
┌──────────────┐    stdio    ┌─────────────────────────────────────────┐
│ Claude Code  │◄───────────►│           dbread MCP Server             │
│ (MCP client) │   (JSON)    │                                         │
└──────────────┘             │  ┌──────────┐                           │
                             │  │  tools   │  list_connections         │
                             │  │          │  list_tables              │
                             │  │          │  describe_table           │
                             │  │          │  query                    │
                             │  │          │  explain                  │
                             │  └────┬─────┘                           │
                             │       │                                 │
                             │  ┌────▼────┐  ┌────────┐  ┌────────┐    │
                             │  │sql_guard│  │rate_lmt│  │ audit  │    │
                             │  └────┬────┘  └───┬────┘  └───┬────┘    │
                             │       │           │           │         │
                             │       └─────┬─────┴─────┬─────┘         │
                             │             │           │               │
                             │        ┌────▼────┐ ┌────▼─────┐         │
                             │        │ config  │ │connection│         │
                             │        │ YAML    │ │ manager  │         │
                             │        └─────────┘ └────┬─────┘         │
                             │                         │               │
                             └─────────────────────────┼───────────────┘
                                                       │
                                      ┌────────────────┼─────────────────┐
                                      ▼                ▼                 ▼
                                 ┌─────────┐      ┌────────┐       ┌────────┐
                                 │Postgres │      │ MySQL  │  ...  │SQLite  │
                                 │(RO user)│      │(RO user│       │(ro mode)│
                                 └─────────┘      └────────┘       └────────┘
\`\`\`

## 5-Layer Defense in Depth

| Layer | Mechanism | Rejects |
|-------|-----------|---------|
| 0 | DB user with GRANT SELECT only | Any write, privileged ops |
| 1 | sqlglot AST validation | DML/DDL/DCL, multi-statement, side-effect functions |
| 2 | Rate limiter + statement_timeout | Runaway loops, long queries |
| 3 | Auto-inject LIMIT N | Oversized result sets |
| 4 | Audit JSONL | (detection, not prevention) |

**Principle:** Never rely on a single layer. Layer 0 is the non-bypassable guarantee.

## Data Flow — Query Tool

\`\`\`
1. MCP client calls: query(connection='x', sql='SELECT ...')
2. tools.query():
   a. guard.validate(sql, dialect)      # Layer 1 - may reject
   b. sql = guard.inject_limit(sql)     # Layer 3
   c. rate_limiter.acquire(connection)  # Layer 2a - may reject
   d. engine.execute(sql)                # hits Layer 2b (DB-side timeout) + Layer 0 (RO user)
   e. audit.log(...)                     # Layer 4
3. Return rows JSON to MCP client
\`\`\`

## File Layout
See `pyproject.toml` and source in `src/dbread/`.

## Design Decisions
- **SQLAlchemy 2.x** — multi-dialect inspector, no hand-rolled metadata queries
- **sqlglot** — 20+ dialects AST, handles CTE-DML edge cases
- **JSONL audit** — append-only, grep/jq friendly, resilient to crash
- **stdio transport** — matches Claude Code MCP native config
- **In-memory rate limit** — single-process scope, simpler than Redis (YAGNI)

## Non-Goals
- Multi-tenancy
- Persistent rate limit (restart = reset)
- NoSQL support (future extension possible)
- Network RPC (stdio only)
```

### Step 3 — `docs/security-threat-model.md`

```markdown
# Security Threat Model (STRIDE)

## Scope
Single-process MCP server on a developer workstation. Client = Claude Code (trusted but prompt-injection prone). Database = external.

## Assets
- Data in database (primary)
- Credentials in `.env` / `config.yaml`
- Audit log (forensic value)

## Trust Boundaries
- Client ↔ MCP server (stdio) — same OS user
- MCP server ↔ DB (network) — DB enforces Layer 0

## STRIDE Table

| Threat | Vector | Mitigation (Layer) | Residual Risk |
|--------|--------|--------------------|----|
| **S**poofing: attacker impersonates MCP server | Malicious binary on PATH | User installs from trusted source; `uv` lockfile pins | Low |
| **T**ampering: modify SQL mid-flight | N/A in stdio local | - | Negligible |
| **T**ampering: AI crafted SQL to bypass guard | Obscure syntax, CTE-DML, comment evasion | Layer 1 (AST walk) + Layer 0 (DB user cannot write) | Low — Layer 0 guarantees |
| **T**ampering: direct DB access bypasses dbread | User shares credentials outside MCP | Docs: use read-only user only via dbread | Out of scope |
| **R**epudiation: "I didn't run that" | - | Layer 4 audit JSONL with timestamp + SQL | Low |
| **I**nformation disclosure: credentials leak | Commit `.env` | `.gitignore` + `url_env` env-var pattern | Low (if docs followed) |
| **I**nformation disclosure: audit contains PII | Raw SQL logged | Docs warn; rotate 50MB; user may scrub | Medium — documented |
| **I**nformation disclosure: sensitive tables readable | DB user granted too broadly | Docs: grant minimum tables/schemas | User-config dependent |
| **D**enial of Service: runaway query | AI loop large queries | Layer 2 rate limit + statement_timeout + LIMIT inject | Low |
| **D**enial of Service: audit fill disk | Unbounded log | Layer 4 rotation 50MB (1 backup = 100MB cap) | Low |
| **E**levation: side-effect fn (pg_read_file, xp_cmdshell) | SELECT wrapping function | Layer 1 function blacklist + Layer 0 (user lacks exec on superuser fns) | Low |
| **E**levation: CTE-DML trick (`WITH d AS (DELETE...) SELECT...`) | PG RETURNING | Layer 1 walks With.expressions + Layer 0 | Low |
| **E**levation: multi-statement injection | `SELECT 1; DROP ...` | Layer 1 rejects len(stmts) > 1 + DB driver flag `multi_statement=false` where available | Low |
| **E**levation: unknown statement type (VACUUM, SET) | Command AST | Layer 1 reject `exp.Command` + Layer 0 | Low |

## Assumption Log
- User follows `docs/setup-db-readonly.md` — **critical**
- Client workstation not compromised (dbread is not a network trust boundary)
- sqlglot keeps up with dialect edge cases (pin version; audit on upgrade)

## Response Plan
- If Layer 1 bypass discovered → Layer 0 prevents damage → patch guard → release
- If audit log shows unusual pattern → grep by conn+reason → correlate with AI session
```

### Step 4 — Expand `README.md`

Replace Phase 01 stub with full quickstart:

```markdown
# dbread

Read-only DB MCP proxy for AI. Gives Claude Code unified SELECT access to multiple databases with guardrails.

## Why
Don't hand raw connection strings to AI. dbread enforces:
1. **DB user read-only** (you configure) — Layer 0
2. **sqlglot AST validation** — rejects DML/DDL/DCL
3. **Rate limit + statement timeout** — prevents runaway
4. **Auto LIMIT injection** — bounded result sets
5. **Audit log** — every query, ok or rejected

See [docs/architecture.md](docs/architecture.md) and [docs/security-threat-model.md](docs/security-threat-model.md).

## Quickstart (5 minutes)

### 1. Install
\`\`\`bash
git clone <repo> dbread && cd dbread
# pick the driver you need (may combine):
uv sync --extra postgres --extra mysql --extra dev
\`\`\`

### 2. Configure read-only DB user
**Do this first.** See [docs/setup-db-readonly.md](docs/setup-db-readonly.md) for your DB.

### 3. Config file
\`\`\`bash
cp config.example.yaml config.yaml
cp .env.example .env
# edit both: set your connection URL (prefer url_env pattern)
\`\`\`

### 4. Register with Claude Code
Add to `~/.claude/mcp_servers.json` (or platform equivalent):

\`\`\`json
{
  "mcpServers": {
    "dbread": {
      "command": "uv",
      "args": ["--directory", "/abs/path/to/dbread", "run", "dbread"],
      "env": {
        "DBREAD_CONFIG": "/abs/path/to/dbread/config.yaml"
      }
    }
  }
}
\`\`\`

### 5. Use it
In Claude Code, just ask: "What tables are in analytics_prod?"

Claude invokes:
- `list_connections` → picks `analytics_prod`
- `list_tables` → shows tables
- `describe_table` → shows columns
- `query` → runs SELECT

Rejected examples (Claude will retry safely):
- `UPDATE users ...` → `sql_guard: node_rejected: Update`
- `WITH d AS (DELETE...) ...` → `sql_guard: node_rejected: Delete`

## Tools

| Tool | Purpose |
|------|---------|
| `list_connections` | Configured connections + dialects |
| `list_tables` | Tables in a connection |
| `describe_table` | Columns, types, indexes |
| `query` | Run SELECT/WITH, auto-limited, audited |
| `explain` | Query plan |

## Audit Log
Append-only JSONL at `audit.path`. One record per query (ok + rejected). Auto-rotates at `rotate_mb`.

\`\`\`bash
jq '.' audit.jsonl                    # pretty
jq 'select(.status=="rejected")' audit.jsonl   # just rejections
\`\`\`

## Development
\`\`\`bash
uv sync --extra dev
uv run pytest
uv run ruff check src/
\`\`\`

## Docs
- [docs/setup-db-readonly.md](docs/setup-db-readonly.md) — Layer 0 DB user setup (mandatory)
- [docs/architecture.md](docs/architecture.md)
- [docs/security-threat-model.md](docs/security-threat-model.md)
```

## Todo List

- [ ] Write `docs/setup-db-readonly.md` — PG, MySQL, MSSQL, Oracle, SQLite sections
- [ ] Test each SQL snippet on real/local DB (PG, MySQL, SQLite minimum)
- [ ] Write `docs/architecture.md` — diagram + 5 layers + data flow
- [ ] Write `docs/security-threat-model.md` — STRIDE table
- [ ] Expand `README.md` — quickstart, Claude Code MCP config, tool list, audit
- [ ] Verify Claude Code MCP config JSON parses + path correct
- [ ] Cross-link all docs

## Success Criteria

- User on fresh machine can follow README + setup-db-readonly.md → working dbread in < 10 min
- Each SQL snippet copy-paste runnable
- STRIDE table covers every risk from brainstorm §6
- Architecture diagram renders in plain text (no Mermaid/image dependency)
- README lists all 5 tools with descriptions

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| SQL snippet fails on specific DB version | Medium | Medium | Test on current LTS (PG 15+, MySQL 8+, MSSQL 2019+, Oracle 19c+); note version in doc |
| User ignores Layer 0 setup | Low-Medium | **High** | README first paragraph + setup doc opening paragraph bold warning |
| MCP config path OS-specific | Medium | Low | Document Windows + macOS + Linux paths in config snippet |
| sqlglot version drift changes guard behavior — docs stale | Medium | Low | Docs reference phases; add "as of sqlglot v23.x" note |

## Security Considerations

- Docs explicitly instruct: **never** use the DB admin user for dbread
- Docs emphasize: `.env` and `config.yaml` gitignored (already from Phase 01)
- Audit log privacy note — user may want to scrub PII/secrets in logged SQL if shared
- MCP config in Claude Code may contain path but not credentials — credentials via env

## Next Steps

- **Blocks:** Phase 07 (integration test references docs Claude Code config)
- **Dependencies:** Phase 01 (stub README), Phase 05 (tool signatures must match)
- **Follow-up:** Optional future — translate to Vietnamese if target audience
