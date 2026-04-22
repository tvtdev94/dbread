<div align="center">

<img src="docs/images/hero-banner.svg" alt="dbread - Read-only DB MCP Proxy for AI" width="100%" />

# `dbread`

### Read-only database MCP proxy for AI — safe `SELECT` access with 5-layer defense

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-1.27+-6e56cf?logo=anthropic&logoColor=white)](https://modelcontextprotocol.io/)
[![Tests](https://img.shields.io/badge/tests-97%20passing-22c55e)](#-testing)
[![Coverage](https://img.shields.io/badge/coverage-80%25-0891b2)](#-testing)
[![Built with uv](https://img.shields.io/badge/built%20with-uv-de5fe9)](https://docs.astral.sh/uv/)
[![License MIT](https://img.shields.io/badge/license-MIT-94a3b8)](LICENSE)

[**Why**](#-why) · [**Quickstart**](#-quickstart-5-minutes) · [**Tools**](#-tools) · [**Security Model**](#%EF%B8%8F-security-model) · [**Docs**](#-docs)

</div>

---

## 🤔 Why

Handing a raw database connection string to an AI is like handing a stranger your car keys. They *probably* won't crash it, but you wouldn't bet the car on it.

**dbread sits between your AI and your DBs** and enforces read-only access through **five independent layers** — if one layer has a bug, the next one still blocks you.

<div align="center">
<img src="docs/images/layers-diagram.svg" alt="5-layer defense in depth" width="100%" />
</div>

---

## ⚡ Quickstart (2 minutes, no clone needed)

### 1. Install as a tool

```bash
# From PyPI (recommended):
uv tool install "dbread[postgres]"          # + any extras: mysql, mssql, oracle

# OR straight from GitHub (no PyPI needed):
uv tool install "git+https://github.com/tvtdev94/dbread[postgres]"
```

### 2. Create a read-only DB user

See [`docs/setup-db-readonly.md`](docs/setup-db-readonly.md) — copy-paste SQL snippets for PostgreSQL / MySQL / MSSQL / Oracle / SQLite.

### 3. Create `config.yaml` + `.env`

```yaml
# ~/.dbread/config.yaml
connections:
  mydb:
    url_env: MYDB_URL
    dialect: postgres
    rate_limit_per_min: 60
    statement_timeout_s: 30
    max_rows: 1000
audit:
  path: ~/.dbread/audit.jsonl
  rotate_mb: 50
```

```
# ~/.dbread/.env
MYDB_URL=postgresql+psycopg2://ai_readonly:password@host:5432/mydb
```

### 4. Register with Claude Code

```bash
claude mcp add --scope user dbread \
  --env DBREAD_CONFIG=/path/to/config.yaml \
  -- dbread
```

Or without install (one-shot via `uvx`):
```bash
claude mcp add --scope user dbread \
  --env DBREAD_CONFIG=/path/to/config.yaml \
  -- uvx --from "dbread[postgres]" dbread
```

### 5. Use it

Restart Claude Code → `/mcp` → `dbread` appears. Ask Claude: *"list connections in dbread, then count rows per status in the orders table."*

<details>
<summary><b>Alternative: clone the repo (for development)</b></summary>

```bash
git clone https://github.com/tvtdev94/dbread && cd dbread
uv sync --extra postgres --extra dev
cp config.example.yaml config.yaml && cp .env.example .env
claude mcp add --scope user dbread -- uv --directory $(pwd) run dbread
```
</details>

Ask Claude: *"List connections in dbread, then count rows per status in the orders table."*

---

## 🏗️ Architecture

<div align="center">
<img src="docs/images/architecture-diagram.svg" alt="dbread architecture" width="100%" />
</div>

**Data flow for a `query` call:**

```mermaid
sequenceDiagram
    participant AI as Claude
    participant T as tools.query
    participant G as SqlGuard
    participant R as RateLimiter
    participant D as Database
    participant A as Audit

    AI->>T: query(sql, connection)
    T->>G: validate(sql, dialect)
    alt SQL is DML/DDL
        G-->>T: rejected
        T->>A: log(rejected, reason)
        T-->>AI: ❌ sql_guard error
    else SQL is SELECT
        G->>T: ✓ plus inject LIMIT N
        T->>R: acquire(connection)
        alt Rate limit hit
            R-->>T: denied
            T->>A: log(rejected, rate_limit)
            T-->>AI: ❌ rate_limit_exceeded
        else Rate limit OK
            R->>T: ✓
            T->>D: execute(sql)
            D-->>T: rows
            T->>A: log(ok, rows, ms)
            T-->>AI: ✅ rows JSON
        end
    end
```

---

## 🧰 Tools

| Tool | Purpose | Input |
|------|---------|-------|
| `list_connections` | Configured connections + dialects | — |
| `list_tables` | Tables in a connection | `connection`, `schema?` |
| `describe_table` | Columns, types, nullability, PKs, indexes | `connection`, `table`, `schema?` |
| `query` | Run `SELECT`/`WITH`/`EXPLAIN`/`SHOW`. Auto-limited. Rate-limited. Audited. | `connection`, `sql`, `max_rows?` |
| `explain` | Query execution plan | `connection`, `sql` |

---

## 🛡️ Security Model

| Layer | Mechanism | What it rejects |
|:-:|---|---|
| **0** | DB user with `GRANT SELECT` only | **All writes — mandatory, non-bypassable** |
| **1** | `sqlglot` AST validation | `INSERT` · `UPDATE` · `DELETE` · `MERGE` · `CREATE` · `ALTER` · `DROP` · `TRUNCATE` · `GRANT` · `REVOKE` · multi-statement (`SELECT 1; DROP...`) · **PG CTE-DML trick** (`WITH d AS (DELETE...) SELECT...`) · function blacklist (`pg_read_file`, `xp_cmdshell`, `load_file`, `dblink_exec`, …) |
| **2** | Rate limit + `statement_timeout` | Runaway loops · long-running queries |
| **3** | Auto-inject `LIMIT N` | Oversized result sets |
| **4** | JSONL audit log | *(detection, not prevention — grep-friendly forensics)* |

> 💡 **Principle:** Never rely on a single layer. Layer 0 is the guarantee; Layers 1–4 make attacks loud and rare.

Full threat model: [`docs/security-threat-model.md`](docs/security-threat-model.md) (STRIDE analysis).

---

## 📋 Example Prompts

```
💬 "List connections in dbread."
💬 "Describe the schema of the orders table in analytics_prod."
💬 "Top 10 customers by lifetime value — use dbread."
💬 "Run EXPLAIN on: SELECT ... ORDER BY created_at"
```

```
💬 "Update user 1 to 'hacked'."
   → ❌ sql_guard: node_rejected: Update

💬 "WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d"
   → ❌ sql_guard: node_rejected: Delete   (PG CTE-DML blocked)

💬 "SELECT 1; DROP TABLE users;"
   → ❌ sql_guard: multi_statement_not_allowed
```

---

## 📜 Audit Log

Every call lands in `audit.jsonl` — one JSON per line, append-only, auto-rotated at 50 MB.

```jsonc
{"ts":"2026-04-22T19:30:12+07:00","conn":"analytics","sql":"SELECT * FROM users LIMIT 100","rows":100,"ms":42,"status":"ok"}
{"ts":"2026-04-22T19:30:15+07:00","conn":"analytics","sql":"DELETE FROM users","rows":0,"ms":0,"status":"rejected","reason":"node_rejected: Delete"}
```

```bash
jq 'select(.status=="rejected")' audit.jsonl     # just rejections
jq 'select(.ms > 1000)' audit.jsonl              # slow queries
jq -s 'group_by(.status)|map({s:.[0].status,n:length})' audit.jsonl   # counts
```

---

## 🗂️ Config

`config.yaml` (gitignored — safe to edit with real values):

```yaml
connections:
  analytics_prod:
    url_env: ANALYTICS_PROD_URL        # credentials from .env
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

Supported dialects: `postgres` · `mysql` · `mssql` · `sqlite` · `oracle`.

---

## 🧪 Testing

```bash
uv sync --extra dev
uv run pytest                          # 97 passing
uv run pytest --cov=dbread             # coverage report
uv run ruff check src/                 # lint

# Integration tests with real PG + MySQL (needs Docker):
cd tests/integration && docker compose up -d
uv run pytest tests/integration/ -v
```

- **89 unit tests** cover config, connections, audit, SQL guard (**48 evasion cases**), rate limiter, tools.
- **4 SQLite E2E tests** always run.
- **4 PG + 4 MySQL E2E tests** skip gracefully without Docker.

---

## 📚 Docs

| Document | What's in it |
|----------|--------------|
| [`docs/setup-db-readonly.md`](docs/setup-db-readonly.md) | **Copy-paste SQL** for Layer 0 DB user on PG / MySQL / MSSQL / Oracle / SQLite |
| [`docs/architecture.md`](docs/architecture.md) | Component diagram · 5-layer details · data flow · design decisions |
| [`docs/security-threat-model.md`](docs/security-threat-model.md) | Full STRIDE analysis · residual risks · response plan |
| [`docs/manual-smoke-test.md`](docs/manual-smoke-test.md) | Step-by-step checklist for verifying integration with Claude Code |

---

## 🧱 Project Layout

```
src/dbread/
├── server.py         # MCP stdio entry — registers 5 tools
├── tools.py          # tool handlers (guard → limit → rate → exec → audit)
├── sql_guard.py      # sqlglot AST validator + LIMIT injection
├── rate_limiter.py   # thread-safe token bucket per connection
├── connections.py    # SQLAlchemy engine manager (lazy, per-dialect)
├── config.py         # pydantic Settings (YAML + env)
└── audit.py          # append-only JSONL with size rotation
```

Every source file is **under 200 LOC** — designed to be readable end-to-end.

---

## 🙏 Credits

Built with [`mcp`](https://modelcontextprotocol.io/) · [`sqlglot`](https://sqlglot.com/) · [`SQLAlchemy 2.x`](https://www.sqlalchemy.org/) · [`pydantic`](https://docs.pydantic.dev/) · [`uv`](https://docs.astral.sh/uv/).

---

<div align="center">
<sub>Made with ❤️ for developers who want AI productivity <strong>without</strong> giving up database safety.</sub>
</div>
