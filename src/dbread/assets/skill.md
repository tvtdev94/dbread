---
name: dbread
description: Provides safe read-only access to the user's databases (PostgreSQL, MySQL, MSSQL, Oracle, SQLite, DuckDB, ClickHouse, MongoDB) via the dbread MCP server. All writes are blocked by a 5-layer guard; only SELECT / WITH / EXPLAIN / SHOW for SQL and find/count/distinct/aggregate for MongoDB succeed. Queries are automatically row-limited, rate-limited, and audited. Also provides setup CLI helpers (`dbread add`, `dbread add-extra`, `dbread doctor`) for adding connections from any common connection-string format and managing driver extras.
when_to_use: The user asks to query a database, run SELECT or MongoDB commands, count or aggregate rows, inspect table schemas or indexes, explore an unfamiliar database, explain a query plan, analyze the audit log, OR add a new database connection / install a missing driver / diagnose a dbread setup issue.
---

# dbread — Read-only Database MCP

dbread is an MCP server giving you safe read-only access to the user's
databases. Every query is validated, rate-limited, row-capped, and audited.
You cannot write, alter, drop, or execute side-effecting functions.

## Golden workflow (follow every time)

Never guess schemas. Always discover first, then query.

```
1. list_connections          → which DBs are configured?
2. list_tables(conn)         → what tables exist?
3. describe_table(conn, tbl) → columns, types, PKs, indexes
4. query(conn, sql|command)  → run the actual read
```

Use `explain` on any query that looks expensive BEFORE running it.

## Tools available

| Tool | Purpose | Key inputs |
|------|---------|------------|
| `list_connections` | Enumerate configured DBs + dialects | — |
| `list_tables` | Tables/collections in one connection | `connection`, `schema?` |
| `describe_table` | Columns · types · PKs · indexes (SQL) · sampled field schema (Mongo) | `connection`, `table`, `schema?` |
| `query` | Run SELECT / WITH / SHOW (SQL) or find/count/distinct/aggregate (Mongo). Auto-limited. | `connection`, `sql` **or** `command`, `max_rows?` |
| `explain` | Execution plan | `connection`, `sql` **or** `command` |

## SQL vs MongoDB routing

Check the dialect returned by `list_connections`:

- **SQL dialects** (`postgres`, `mysql`, `mssql`, `sqlite`, `oracle`, `duckdb`, `clickhouse`):
  pass `sql` — standard SELECT / WITH / EXPLAIN / SHOW.
  ```json
  {"connection": "analytics", "sql": "SELECT status, COUNT(*) FROM orders GROUP BY status"}
  ```

- **MongoDB** (`mongodb`): pass `command` as a JSON object.
  Allowed: `find` · `count` · `countDocuments` · `estimatedDocumentCount` · `distinct` · `aggregate`.
  Blocked (will error): `$out`, `$merge`, `$function`, `$accumulator`, `$where`, `mapReduce`, `$unionWith`, cross-DB `$lookup`.
  ```json
  {"connection": "analytics_mongo",
   "command": {"aggregate": "users",
               "pipeline": [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]}}
  ```

Never mix: if dialect is `mongodb`, do not send `sql`. If SQL dialect, do not send `command`. The server rejects cross-mismatch.

## Error handling — how to recover

| Error pattern | Cause | What to do |
|---------------|-------|------------|
| `sql_guard: node_rejected: <Update\|Delete\|Insert\|...>` | Tried DML/DDL | Explain to user: dbread is read-only. Do not retry. |
| `sql_guard: multi_statement_not_allowed` | Semicolon-separated statements | Split into separate `query` calls. |
| `sql_guard: function_blacklisted: <name>` | Used dangerous function (`pg_read_file`, `xp_cmdshell`, ClickHouse `url/s3/remote`, DuckDB `read_csv`, etc.) | Rewrite without that function. |
| `mongo_guard: blocked_operator: $out` | Pipeline contains write stage | Remove write stage; use aggregate that returns data instead. |
| `mongo_guard: command_not_allowed: <name>` | Used non-allowlisted command | Switch to find/count/distinct/aggregate. |
| `rate_limit_exceeded: per_conn` | Too many queries on this connection this minute | Wait ~60s, then retry. Consolidate queries if possible. |
| `rate_limit_exceeded: global` | Total QPM across all connections hit | Wait and retry; reduce query fan-out. |
| `db_error: ... timeout ...` | Query exceeded `statement_timeout_s` | Add WHERE filters, LIMIT, or specific columns. Run `explain` first. |
| `truncated: true` in response | Result hit `max_rows` cap | Warn user that results are partial; suggest narrower WHERE or pagination. |

## Refusing writes

If the user asks to write, insert, update, delete, or migrate data, refuse
politely and explain that dbread is read-only by design. Do not attempt the
operation — the guard blocks it deterministically. Suggest the user run the
mutation through a tool with write privileges.

## Query patterns that work well

- **Always name columns** — prefer `SELECT id, email, status FROM users` over `SELECT *`.
- **Add a LIMIT** even though dbread auto-injects one — makes intent explicit.
- **Use EXPLAIN first** for unfamiliar tables or joins across 3+ tables.
- **For counts**, use `COUNT(*)` with a tight WHERE; don't pull rows just to count them.
- **For Mongo**, prefer `$match` early in pipelines (before `$lookup`/`$group`) for index use.

## Privacy note

Every `query` and `explain` call is logged to an audit JSONL file. If the
user's config has `redact_literals: false` (default), your literal WHERE
values are stored. When the user mentions PII (emails, names, IDs) in a
filter, consider suggesting they enable `redact_literals: true` in their
config.

## Example good interactions

**User**: "Show me active users in analytics."
**You**:
1. `list_connections` → confirm `analytics` exists and its dialect.
2. `list_tables(analytics)` → confirm `users` table.
3. `describe_table(analytics, users)` → learn column `status`.
4. `query(analytics, "SELECT id, email, created_at FROM users WHERE status = 'active' LIMIT 100")`.
5. Summarize result; note if `truncated: true`.

**User**: "Delete inactive users."
**You**: Refuse. dbread blocks all writes. Suggest the user run that DELETE manually through a tool with write privileges — dbread explicitly does not support it for safety.

## Setup helpers (when user wants to add / fix a connection)

dbread ships with a small CLI for setup — surface these instead of asking the user to hand-edit YAML.

| User says... | Tell them to run | What it does |
|---|---|---|
| "Add my postgres / mysql / etc. to dbread" | `dbread add` | Interactive wizard: paste any connection string (URI / JDBC / ADO.NET / ODBC / MongoDB Atlas / file path), auto-detects format, converts to SQLAlchemy URL, tests live, writes `.env` + `config.yaml`. |
| "I have a `Server=...;Database=...;User Id=...;` string" / JDBC URL / etc. | `dbread add` and paste it | Same as above — handles all 6 format families. |
| "Auto-detect doesn't recognise my string" | `dbread add --manual --dialect-hint <pg\|mysql\|mssql\|...>` | Skips detection; prompts for SQLAlchemy URL directly. Wizard also offers a fallback menu automatically when detection fails. |
| "I want to install another DB driver" | `dbread add-extra <name>` (e.g. `mongo`, `mssql`) | Adds the extra without dropping previously-installed ones (bare `uv tool install dbread[mongo]` WOULD drop them). |
| "Is my dbread setup OK?" / "Why is my connection failing?" | `dbread doctor` | Per-connection table — checks driver is importable, **live-pings each DB** (5s timeout, in parallel), shows summary stats (`X/Y connected`), and prints smart fix hints based on the error pattern (refused / auth / DB missing / SSL / missing driver). Add `--quick` to skip live tests. Auto-loads `~/.dbread/.env` first. |
| "What drivers are installed?" | `dbread list-extras` | Table of tracked vs actually-importable extras + install method. |

Recognised connection-string formats (all 8 dialects): native URI · JDBC · ADO.NET / C# / .NET · ODBC · `mongodb+srv://` (Atlas) · MotherDuck `md:` · file paths (`*.db`, `*.sqlite`, `*.duckdb`).

Unsupported (wizard hard-fails with hint): `Trusted_Connection=yes` (Windows auth) · Oracle TNS descriptor `(DESCRIPTION=...)` · MSSQL named instance `HOST\SQLEXPRESS`.

## Troubleshooting

### Missing driver errors

If a query returns an error like `ModuleNotFoundError: No module named 'psycopg2'` or similar driver import failure,
the connection's dialect needs an extra driver installed. Tell the user to run:

```bash
dbread doctor          # see which drivers are missing
dbread add-extra <name>  # install (e.g. add-extra mongo)
```

`dbread add-extra` is safe to run multiple times — it preserves all previously-installed extras (a bare
`uv tool install dbread[mongo]` would NOT preserve them).

### Connection refused / wrong host / wrong creds

The user's `.env` or `config.yaml` likely has stale values. Suggest:

```bash
dbread add <name>      # re-add the connection; wizard tests live before saving
                       # if name already exists, wizard prompts to overwrite
```

Or they can edit `~/.dbread/.env` directly — the variable name matches `<NAME>_URL` from `config.yaml`.

### Upgrading dbread (v0.7.2+)

```bash
uv tool upgrade dbread       # upgrades the package
```

This skill (`~/.claude/skills/dbread/SKILL.md`) auto-refreshes on the **next**
`dbread` invocation if the bundled version differs — no manual
`dbread install-skill --force` needed. Tell the user to restart Claude Code
afterwards so the new skill is loaded for the current session.

### Upgrade fails on Windows: `os error 32` / "being used by another process"

This is the file-lock error users hit when running `uv tool upgrade dbread` on
Windows while Claude Code is open — `dbread.exe` is locked by the running MCP
server. Tell them to:

```powershell
# Quit Claude Code completely (not just minimize), then:
Get-Process dbread -ErrorAction SilentlyContinue | Stop-Process -Force
uv tool upgrade dbread
# Then reopen Claude Code.
```

Linux/macOS don't have this restriction (running binaries can be replaced).

## Don't do

- Don't call `query` before `describe_table` unless the user explicitly lists column names.
- Don't retry a query that failed with `sql_guard` — the guard is deterministic; it will fail again.
- Don't chain many small queries when a single JOIN/aggregate answers the question — respect rate limits.
- Don't send raw user input as SQL values without quoting; use parameterized patterns when possible (though dbread's limits mitigate most injection impact, clean queries are still better).
- Don't assume a MongoDB collection has a consistent schema — `describe_table` returns a **sampled** schema; rare fields may be missing.
