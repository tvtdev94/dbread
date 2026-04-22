# Manual Smoke Test - Claude Code MCP Integration

Run this once after setup to confirm end-to-end integration with Claude Code.

## Prerequisites

- [ ] `uv sync --extra <your-drivers>` completed successfully
- [ ] Read-only DB user created (see `setup-db-readonly.md`)
- [ ] `config.yaml` filled with at least one connection (or `url_env` + matching `.env`)
- [ ] `uv run dbread` starts without error (it will hang waiting for stdin — Ctrl+C to exit)

## Register with Claude Code

Add to your Claude Code MCP config:

```json
{
  "mcpServers": {
    "dbread": {
      "command": "uv",
      "args": ["--directory", "/ABS/PATH/TO/dbread", "run", "dbread"],
      "env": {
        "DBREAD_CONFIG": "/ABS/PATH/TO/dbread/config.yaml"
      }
    }
  }
}
```

Restart Claude Code.

## Test Script

Ask Claude Code each of the following, in order:

### 1. Discovery

> **Prompt:** "Use the dbread MCP server to list available connections."

**Expected:** Claude calls `list_connections`, shows your configured names + dialects.

### 2. Schema

> **Prompt:** "List the tables in the `<your-conn-name>` connection."

**Expected:** `list_tables` returns your actual tables.

> **Prompt:** "Describe the schema of the `users` table."

**Expected:** `describe_table` returns columns + types + pk/indexes.

### 3. Happy path query

> **Prompt:** "Count the rows in the `users` table."

**Expected:** `query` runs `SELECT COUNT(*) FROM users` (or similar), returns a number.

### 4. Guard reject path

> **Prompt:** "Update user id 1 to have name 'eve'."

**Expected:** Claude attempts `UPDATE ...`, receives `sql_guard: node_rejected: Update`, explains it cannot write.

### 5. CTE-DML reject (PostgreSQL only)

> **Prompt:** "Run this SQL: `WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d`."

**Expected:** `sql_guard: node_rejected: Delete` — the CTE trick is caught.

### 6. Rate limit

> **Prompt:** "Run `SELECT 1` 100 times in a loop."

**Expected:** After the configured `rate_limit_per_min` is hit, subsequent calls fail with `rate_limit_exceeded`.

### 7. Audit verification

```bash
tail -20 audit.jsonl | jq .
```

**Expected:** You see entries for each successful + rejected call, with timestamps, status, and reason.

## Troubleshooting

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| Claude: "dbread server not responding" | `uv run dbread` fails to start | Run it manually in a terminal; check stderr |
| All queries rejected with `parse_error` | Wrong `dialect` in `config.yaml` | Match dialect to driver (postgres / mysql / sqlite / mssql / oracle) |
| `db_error: FATAL: password authentication failed` | Wrong credentials or DB user not created | Re-check `setup-db-readonly.md` step 1-2 |
| `db_error: permission denied for table users` | Layer 0 works (intended), but GRANT SELECT missing | Run the GRANT step in `setup-db-readonly.md` |
| stdout garbage in MCP handshake | A `print()` leaked into stdout somewhere | Only `sys.stderr` allowed for logs; grep `print(` in `src/` |

## Pass/Fail Criteria

All 7 steps succeed as expected → integration PASS. Any failure → see Troubleshooting or open an issue.
