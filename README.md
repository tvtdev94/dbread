# dbread

Read-only DB MCP proxy for AI. Gives Claude Code unified `SELECT` access to multiple databases with guardrails.

## Why

Don't hand raw connection strings to AI. dbread enforces **defense in depth**:

1. **DB user read-only** (you configure) — Layer 0
2. **sqlglot AST validation** — rejects DML / DDL / DCL
3. **Rate limit + statement timeout** — prevents runaway loops
4. **Auto `LIMIT` injection** — bounded result sets
5. **Audit log** — every query, `ok` or `rejected`

See [docs/architecture.md](docs/architecture.md) and [docs/security-threat-model.md](docs/security-threat-model.md).

## Quickstart (5 minutes)

### 1. Install

```bash
git clone <repo> dbread && cd dbread
# pick the driver(s) you need (combine freely):
uv sync --extra postgres --extra mysql --extra dev
```

### 2. Configure the read-only DB user (do this first)

See [docs/setup-db-readonly.md](docs/setup-db-readonly.md) for your DB engine. **This is the non-bypassable guarantee** — dbread's guard is a belt, the DB user is the suspenders.

### 3. Config file

```bash
cp config.example.yaml config.yaml
cp .env.example .env
# edit both: set your connection URL (prefer the url_env pattern)
```

### 4. Register with Claude Code

Add to `~/.claude/mcp_servers.json` (or the platform equivalent):

```json
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
```

### 5. Use it

In Claude Code, just ask: *"What tables are in analytics_prod?"*

Claude will invoke:

- `list_connections` → picks `analytics_prod`
- `list_tables` → returns the table list
- `describe_table` → shows columns and indexes
- `query` → runs a `SELECT`

Rejected examples (Claude learns to adapt):

- `UPDATE users ...` → `sql_guard: node_rejected: Update`
- `WITH d AS (DELETE ...) SELECT ...` → `sql_guard: node_rejected: Delete`
- `SELECT 1; DROP TABLE x` → `sql_guard: multi_statement_not_allowed`

## Tools

| Tool | Purpose |
|------|---------|
| `list_connections` | Configured connections + dialects |
| `list_tables` | Tables in a connection |
| `describe_table` | Columns, types, indexes |
| `query` | Run `SELECT` / `WITH`. Auto-limited. Rate-limited. Audited. |
| `explain` | Query plan (`EXPLAIN`) |

## Audit Log

Append-only JSONL at the `audit.path` from your config. One record per query (ok + rejected). Auto-rotates at `rotate_mb`.

```bash
jq '.' audit.jsonl                            # pretty
jq 'select(.status=="rejected")' audit.jsonl  # just rejections
jq 'select(.ms > 1000)' audit.jsonl           # slow queries
```

## Development

```bash
uv sync --extra dev
uv run pytest
uv run pytest --cov=dbread --cov-report=term
uv run ruff check src/
```

## Docs

- [docs/setup-db-readonly.md](docs/setup-db-readonly.md) — Layer 0 DB user setup (**mandatory**)
- [docs/architecture.md](docs/architecture.md) — system design and data flow
- [docs/security-threat-model.md](docs/security-threat-model.md) — STRIDE analysis

## Status

`v0.1.0` — core features complete: 5 MCP tools, 5-layer defense, 89+ unit tests. See `plans/` for roadmap.
