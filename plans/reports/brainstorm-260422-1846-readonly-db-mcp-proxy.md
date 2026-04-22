---
type: brainstorm
date: 2026-04-22 18:46 +07
slug: readonly-db-mcp-proxy
status: design-approved
---

# Brainstorm: Read-only DB MCP Proxy for AI

## 1. Problem Statement

Đưa connection string trực tiếp cho AI (Claude Code) → rủi ro:
- AI vô ý / bị prompt-injection chạy `UPDATE`, `DELETE`, `DROP`
- Không giới hạn query → runaway loop đốt DB resource
- Không audit → không forensic được khi có sự cố
- Multi-DB → cần cơ chế thống nhất

## 2. Requirements

**Functional:**
- AI (Claude Code MCP) query được nhiều DB (PG, MySQL, MSSQL, SQLite...)
- Raw SQL tự do nhưng CHỈ đọc (SELECT / EXPLAIN / SHOW / DESCRIBE)
- Rate limit: 60 queries/min/connection, statement timeout 30s
- Audit log JSONL mọi query (cả reject)

**Non-functional:**
- Local, personal/small-team scope (KISS - không cần multi-tenant)
- Setup < 10 phút trên máy mới
- Defense in depth - không tin 1 lớp duy nhất
- File size < 200 LOC/file

## 3. Approaches Evaluated

| # | Approach | Pros | Cons | Verdict |
|---|----------|------|------|---------|
| A | Existing MCP servers (`server-postgres` + community MySQL) | Zero code, battle-tested | Không unified rate limit/audit, phải config rời rạc từng DB | Rejected - thiếu audit |
| B | **Custom Python MCP server** (sqlglot + SQLAlchemy) | 1 tool unified, full control, multi-DB qua SQLAlchemy, audit/rate limit built-in | ~500 LOC phải maintain, parser edge cases | **SELECTED** |
| C | Hybrid (existing PG + custom cho khác) | Tận dụng existing | UX không nhất quán, 2 tool | Rejected - ko lý do mạnh |

## 4. Final Design (Approach B)

### 4.1 Defense in Depth (Non-Negotiable)

```
┌─────────────────────────────────────────────────────┐
│ Layer 0: DB user READ-ONLY (GRANT SELECT only)      │  ← LAST LINE
│ Layer 1: sqlglot AST validation (reject DML/DDL/DCL)│
│ Layer 2: Rate limiter + statement_timeout           │
│ Layer 3: Row limit auto-inject (LIMIT 1000)         │
│ Layer 4: Audit log JSONL                            │
└─────────────────────────────────────────────────────┘
```

**Key principle:** Dù parser có bug, Layer 0 (DB user) vẫn chặn. Không BAO GIỜ chỉ tin parser.

### 4.2 Architecture

```
Claude Code (MCP client)
    ↓ stdio (MCP protocol)
dbread MCP Server (Python)
    ├─ config.yaml     # connection list
    ├─ sql_guard       # sqlglot AST validate
    ├─ rate_limiter    # token bucket in-memory
    ├─ audit           # JSONL append
    └─ SQLAlchemy engines (lazy init)
         ↓
    [Postgres|MySQL|MSSQL|SQLite|Oracle|...]
    (read-only user - mandatory)
```

### 4.3 Tech Stack

| Component | Choice | Reason |
|-----------|--------|--------|
| Language | Python 3.11+ | MCP SDK mature, rich DB ecosystem |
| Package mgr | `uv` | Fast, modern, lockfile |
| MCP | `mcp` (Anthropic SDK) | Official |
| SQL parse | `sqlglot` | 20+ dialect AST, handles CTE edge cases |
| DB abstraction | `SQLAlchemy 2.x` | Multi-DB inspector for list/describe |
| Config | `pydantic-settings` + YAML | Typed, validated |
| Drivers | `psycopg2-binary`, `pymysql`, `pyodbc`, `oracledb` | Optional via extras |

### 4.4 File Structure (all < 200 LOC)

```
dbread/
├── pyproject.toml
├── README.md                       # quickstart + Claude Code MCP config
├── config.example.yaml
├── .env.example
├── .gitignore                      # config.yaml, *.jsonl, .env
├── src/dbread/
│   ├── __init__.py
│   ├── server.py                   # MCP entry (~100)
│   ├── config.py                   # pydantic models (~80)
│   ├── connections.py              # SA engine manager (~100)
│   ├── sql_guard.py                # sqlglot validator (~150)
│   ├── rate_limiter.py             # token bucket (~80)
│   ├── audit.py                    # JSONL writer (~60)
│   └── tools.py                    # MCP tool handlers (~180)
├── docs/
│   ├── setup-db-readonly.md        # GRANT SELECT per-DB (PG/MySQL/MSSQL/Oracle)
│   ├── architecture.md
│   └── security-threat-model.md
└── tests/
    ├── test_sql_guard.py           # CRITICAL: evasion attempts
    ├── test_rate_limiter.py
    └── test_connections.py
```

### 4.5 MCP Tools Exposed

| Tool | Input | Output |
|------|-------|--------|
| `list_connections` | - | tên + dialect các DB configured |
| `list_tables` | `connection`, optional `schema` | danh sách bảng |
| `describe_table` | `connection`, `table` | columns + types + indexes |
| `query` | `connection`, `sql`, optional `max_rows` | rows JSON |
| `explain` | `connection`, `sql` | query plan |

### 4.6 SQL Guard Rules (sqlglot AST)

**ALLOW:** `Select`, `With` (nếu không chứa modification CTE), `Describe`, `Show`, `Explain`

**REJECT:**
- `Insert`, `Update`, `Delete`, `Merge`
- `Create`, `Alter`, `Drop`, `Truncate`, `Rename`
- `Grant`, `Revoke`
- `Call` (stored proc - có thể có side effect)
- Multi-statement (`SELECT 1; DROP TABLE x`)
- PG-specific: CTE chứa `INSERT/UPDATE/DELETE` trong `WITH` (`WITH x AS (DELETE...) SELECT...`)
- Blacklist functions gây side effect: `pg_advisory_lock`, `lo_import`, `pg_read_file`, `lo_export`, `dblink_exec`, `xp_cmdshell` (MSSQL)

**AUTO-INJECT:** `LIMIT <max_rows>` nếu SELECT top-level không có LIMIT

### 4.7 Config Sample

```yaml
connections:
  analytics_prod:
    url_env: ANALYTICS_PROD_URL      # từ env, không hardcode
    dialect: postgres
    rate_limit_per_min: 60
    statement_timeout_s: 30
    max_rows: 1000
  local_mysql:
    url: mysql+pymysql://readonly:pw@localhost/shop
    dialect: mysql
    rate_limit_per_min: 120
    statement_timeout_s: 15
    max_rows: 500

audit:
  path: ./audit.jsonl
  rotate_mb: 50
```

### 4.8 Audit Record Format

```json
{"ts":"2026-04-22T18:47:00+07:00","conn":"analytics_prod","sql":"SELECT * FROM users LIMIT 10","rows":10,"ms":42,"status":"ok"}
{"ts":"...","conn":"analytics_prod","sql":"DELETE FROM users","rows":0,"ms":0,"status":"rejected","reason":"sql_guard: DML not allowed"}
```

## 5. DB Read-Only Setup (Layer 0 docs)

### PostgreSQL
```sql
CREATE USER ai_readonly WITH PASSWORD 'strong_pw';
GRANT CONNECT ON DATABASE mydb TO ai_readonly;
GRANT USAGE ON SCHEMA public TO ai_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ai_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO ai_readonly;
ALTER USER ai_readonly SET default_transaction_read_only = on;
ALTER USER ai_readonly SET statement_timeout = '30s';
```

### MySQL
```sql
CREATE USER 'ai_readonly'@'%' IDENTIFIED BY 'strong_pw';
GRANT SELECT, SHOW VIEW ON mydb.* TO 'ai_readonly'@'%';
SET GLOBAL max_execution_time = 30000;  -- per-session better
```

### MSSQL
```sql
CREATE LOGIN ai_readonly WITH PASSWORD = 'Strong_Pw!';
USE mydb;
CREATE USER ai_readonly FOR LOGIN ai_readonly;
ALTER ROLE db_datareader ADD MEMBER ai_readonly;
DENY EXECUTE TO ai_readonly;  -- block stored procs
```

### Oracle
```sql
CREATE USER ai_readonly IDENTIFIED BY "strong_pw";
GRANT CREATE SESSION TO ai_readonly;
GRANT SELECT ANY TABLE TO ai_readonly;
ALTER USER ai_readonly PROFILE readonly_profile;  -- with IDLE_TIME etc
```

Chi tiết sẽ viết đầy đủ trong `docs/setup-db-readonly.md` khi implement.

## 6. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| sqlglot bypass (obscure syntax) | Medium | High nếu chỉ có parser | Layer 0 DB user là net cuối → High→Low |
| Connection string leak | Low | Critical | `.env` gitignored, config.yaml gitignored, `url_env` pattern |
| Rate limit bypass (spawn nhiều MCP session) | Low | Medium | Token bucket per connection_name global trong process |
| Long-running query | Medium | Medium | statement_timeout DB-side + SA pool_timeout |
| Audit log grow vô hạn | High | Low | Rotate 50MB |
| Side-effect functions qua SELECT | Low | Medium | Function blacklist + `default_transaction_read_only` |
| Multi-statement SQL injection | Low | High | Reject nếu AST có > 1 statement |

## 7. Success Criteria

- [ ] Claude Code gọi được 5 tools qua MCP
- [ ] Query `SELECT * FROM users LIMIT 5` → trả rows
- [ ] Query `UPDATE users SET x=1` → rejected by guard
- [ ] Query `WITH d AS (DELETE FROM x RETURNING *) SELECT * FROM d` → rejected (PG CTE trick)
- [ ] Query `SELECT 1; DROP TABLE x` → rejected (multi-statement)
- [ ] 61 queries/min → 61 request rate-limited
- [ ] Query > 30s → timeout DB-side
- [ ] Audit log có entry cho cả ok và rejected
- [ ] Setup doc chạy được trên PG, MySQL, MSSQL, SQLite
- [ ] Unit test cho sql_guard cover 15+ evasion attempts

## 8. Implementation Phases (Preview)

1. **Setup** - uv init, pyproject, .gitignore, skeleton
2. **Core** - config, connections, audit (foundation)
3. **SQL Guard** - sqlglot validator + comprehensive tests (critical path)
4. **Rate Limiter** - token bucket + tests
5. **MCP Tools** - wire 5 tools với guard + limiter
6. **Docs** - setup-db-readonly per-DB, quickstart README
7. **Integration test** - local PG+MySQL+SQLite docker-compose

## 9. Next Steps

1. User approve design → tạo plan chi tiết qua `/ck:plan`
2. Plan gồm 7 phases như trên, mỗi phase 1 file phase-XX.md
3. Implement theo primary workflow (planner → impl → tester → reviewer)

## 10. Open Questions

- Có cần schema-level filter không? (ví dụ: chỉ allow query schema `public`, block `pg_catalog`) - hiện tại default allow all, có thể add sau nếu cần.
- Có cần encryption-at-rest cho audit log không? → Local scope, YAGNI - skip.
- Support NoSQL (Mongo, Redis) không? → Scope hiện tại chỉ SQL. Nếu cần sau sẽ extend.
