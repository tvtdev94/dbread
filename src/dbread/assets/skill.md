---
name: dbread
description: Provides safe read-only access to the user's databases (PostgreSQL, MySQL, MSSQL, Oracle, SQLite, DuckDB, ClickHouse, MongoDB) via the dbread MCP server. All writes are blocked by a 5-layer guard; only SELECT / WITH / EXPLAIN / SHOW for SQL and find/count/distinct/aggregate for MongoDB succeed. Also offers sample_table and profile_table for fast data debugging. Queries are automatically row-limited, rate-limited, and audited. Also provides setup CLI helpers (`dbread add`, `dbread add-extra`, `dbread doctor`) for adding connections from any common connection-string format and managing driver extras.
when_to_use: The user asks to query a database, run SELECT or MongoDB commands, count or aggregate rows, inspect table schemas or indexes, explore an unfamiliar database, preview recent rows, check data quality such as null rates or duplicates, explain a query plan, analyze the audit log, OR add a new database connection / install a missing driver / diagnose a dbread setup issue.
---

# dbread — Read-only Database MCP

dbread is an MCP server giving you safe read-only access to the user's
databases. Every query is validated, rate-limited, row-capped, and audited.
You cannot write, alter, drop, or execute side-effecting functions.

## Golden workflow (follow every time)

Never guess schemas. Always discover first, then query.

```
1. list_connections          → which DBs are configured?
2. list_tables(conn)         → what relations exist? (tables, views, matviews)
3. describe_table(conn, tbl) → columns, types, PKs, indexes, foreign keys
4. query(conn, sql|command)  → run the actual read
```

Use `explain` on any query that looks expensive BEFORE running it.

When the user is *debugging data* rather than asking a precise question,
start with `sample_table` to see real rows, then `profile_table` to find the
column holding bad data. Both take one call instead of several hand-written
queries against the rate budget.

## Tools available

| Tool | Purpose | Key inputs |
|------|---------|------------|
| `list_connections` | Enumerate configured DBs + dialects | — |
| `list_tables` | Relations in one connection. Returns `{name, type}` — `table` · `view` · `materialized_view` · `collection` | `connection`, `schema?` |
| `list_schemas` | Schema names (Mongo: the one pinned database) | `connection` |
| `describe_table` | Columns · types · PKs · defaults · indexes · **foreign keys** (SQL) · sampled field schema (Mongo) | `connection`, `table`, `schema?` |
| `sample_table` | Most recent rows, auto-ordered by timestamp column, else PK desc (`_id` on Mongo) | `connection`, `table`, `n?`, `schema?` |
| `profile_table` | Per column: null count + %, distinct count, min/max. Sampled. | `connection`, `table`, `columns?`, `schema?`, `sample_size?` |
| `query` | Run SELECT / WITH / SHOW (SQL) or find/count/distinct/aggregate (Mongo). Auto-limited. | `connection`, `sql` **or** `command`, `max_rows?`, `params?` |
| `explain` | Execution plan | `connection`, `sql` **or** `command` |

## SQL vs MongoDB routing

Check the dialect returned by `list_connections`:

- **SQL dialects** (`postgres`, `mysql`, `mssql`, `sqlite`, `oracle`, `duckdb`, `clickhouse`):
  pass `sql` — standard SELECT / WITH / EXPLAIN / SHOW.
  ```json
  {"connection": "analytics", "sql": "SELECT status, COUNT(*) FROM orders GROUP BY status"}
  ```
  Optionally pass `params` for `:name` placeholders instead of inlining
  values. Parameterized statements skip automatic LIMIT injection, so write
  your own LIMIT when you use them.
  ```json
  {"connection": "analytics",
   "sql": "SELECT id, email FROM users WHERE status = :st LIMIT 50",
   "params": {"st": "active"}}
  ```

- **MongoDB** (`mongodb`): pass `command` as a JSON object.
  Commands: `find` · `count` · `countDocuments` · `estimatedDocumentCount` · `distinct` · `aggregate`.

  `find` honors `filter`, `projection`, `sort`, `skip`, `limit`, `hint`,
  `collation`, `batchSize` and `comment`. Any other key is **rejected**, not
  ignored — so a command that succeeds did exactly what you asked.
  ```json
  {"connection": "analytics_mongo",
   "command": {"find": "orders", "filter": {"status": "paid"},
               "sort": {"created_at": -1}, "limit": 10}}
  ```

  Blocked (will error): `$out`, `$merge`, `$function`, `$accumulator`,
  `$where`, `mapReduce`, cross-DB `$lookup` / `$unionWith`.

  `$unionWith`, `$setWindowFields`, `$unset`, `$geoNear`, `$collStats` and
  `$indexStats` are allowed. `$indexStats` needs a grant beyond the plain
  `read` role and says so clearly when the role lacks it.

Never mix: if dialect is `mongodb`, do not send `sql`. If SQL dialect, do not send `command`. The server rejects cross-mismatch.

**Pagination.** Page one: `sort` + `limit`. Deeper pages: prefer a range
filter on the sort key (`{"_id": {"$gt": <last id>}}`) over a large `skip`,
which the server has to walk.

## Error handling — how to recover

| Error pattern | Cause | What to do |
|---------------|-------|------------|
| `sql_guard: node_rejected: <Update\|Delete\|Insert\|...>` | Tried DML/DDL | Explain to user: dbread is read-only. Do not retry. |
| `sql_guard: multi_statement_not_allowed` | Semicolon-separated statements | Split into separate `query` calls. |
| `sql_guard: function_blacklisted: <name>` | Used dangerous function (`pg_read_file`, `xp_cmdshell`, ClickHouse `url/s3/remote`, DuckDB `read_csv`, etc.) | Rewrite without that function. |
| `sql_guard: row_lock_not_allowed` | `SELECT ... FOR UPDATE/SHARE` | Drop the locking clause; a read never needs it. |
| `sql_guard: top_level_not_allowed: Use` | `USE <db>` | One connection is pinned to one database. Ask the user to configure another connection. |
| `mongo_guard: blocked_operator: $out` | Pipeline contains write stage | Remove write stage; use aggregate that returns data instead. |
| `mongo_guard: command_not_allowed: <name>` | Used non-allowlisted command | Switch to find/count/distinct/aggregate. |
| `mongo_guard: field_not_allowed: <key>` | Command carries a key `find`/`aggregate` does not accept | Remove the key. It is rejected rather than ignored so results are never silently wrong. |
| `mongo_guard: <field>_must_be_dict` | e.g. `sort` sent as a string | Send a document: `{"created_at": -1}`. |
| `db_error: ... not authorized ... $indexStats` | Role lacks the `indexStats` action | Tell the user; see `docs/setup-db-readonly.md` for the grant. |
| `unknown_table: <name>` | `sample_table`/`profile_table` on a missing relation | Run `list_tables` first; check the `schema` argument. |
| `rate_limit_exceeded: per_conn` | Too many queries on this connection this minute | Wait ~60s, then retry. Consolidate queries if possible. |
| `rate_limit_exceeded: global` | Total QPM across all connections hit | Wait and retry; reduce query fan-out. |
| `db_error: ... timeout ...` | Query exceeded `statement_timeout_s` | Add WHERE filters, LIMIT, or specific columns. Run `explain` first. |
| `cost_guard_error: rows_estimate=N exceeds <threshold>` (SQL) or `cost_guard_error: docs_estimate=N exceeds <threshold>` (Mongo) | Pre-exec EXPLAIN says the query would scan/return more rows than the user's `max_rows_estimate` cap (Layer 2.5; postgres / mysql / mssql / oracle / duckdb / mongodb when configured) | **Do NOT retry.** Suggest narrower WHERE filters, an indexed column, or aggregation (`COUNT(*)` instead of full SELECT). If the user genuinely needs the data, they must raise `max_rows_estimate` in `config.yaml` themselves. |
| `truncated: true` in response | Result hit `max_rows` cap | Warn user that results are partial; suggest narrower WHERE or pagination. |

## Refusing writes

If the user asks to write, insert, update, delete, or migrate data, refuse
politely and explain that dbread is read-only by design. Do not attempt the
operation — the guard blocks it deterministically. Suggest the user run the
mutation through a tool with write privileges.

## Query patterns that work well

- **Always name columns** — prefer `SELECT id, email, status FROM users` over `SELECT *`.
- **Add a LIMIT** even though dbread auto-injects one — makes intent explicit. An
  oversized LIMIT is clamped down to the connection's `max_rows`.
- **Use EXPLAIN first** for unfamiliar tables or joins across 3+ tables.
- **For counts**, use `COUNT(*)` with a tight WHERE; don't pull rows just to count them.
- **For Mongo**, prefer `$match` early in pipelines (before `$lookup`/`$group`) for index use.
- **Follow the foreign keys** from `describe_table` rather than guessing join columns.
- **Reach for `profile_table`** when the user says data "looks wrong", has
  duplicates, or has unexpected gaps — it answers which column in one call.

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

**User**: "The orders data looks wrong, can you check?"
**You**:
1. `sample_table(analytics, orders)` → see real recent rows.
2. `profile_table(analytics, orders)` → spot the problem column (high null %,
   a distinct count of 1, an out-of-range min/max).
3. `query(...)` targeted at that column to show concrete bad rows.
4. Report what you found and note the numbers come from a sample.

**User**: "Show me the 10 newest orders in the Mongo store."
**You**: `query(analytics_mongo, command={"find": "orders", "sort": {"created_at": -1}, "limit": 10})`.
Sorting is applied by the server; if you pass a key the command does not
support you get `field_not_allowed` rather than quietly unsorted rows.

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
| "Test a query without going through Claude" | `dbread query <conn> "SELECT ..."` | One-shot run with the SAME guard / cost-guard / rate-limit / audit pipeline. TTY → ASCII table; pipe → JSONL (`\| jq`); `--format csv` for export; `--explain` for plan only. Exit codes: `0` ok, `2` guard reject, `3` rate limit, `4` connection error. |
| "Upgrade dbread without losing my drivers" | `dbread upgrade` (or `dbread upgrade --check` for dry-run) | Reinstalls preserving tracked extras (`uv tool upgrade dbread` would drop them). Auto-detects Windows file-lock conflict; `--force-windows` to bypass; `--check` prints current vs PyPI latest without installing. |

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

### Upgrading dbread (v0.8.0+)

```bash
dbread upgrade               # preserves tracked extras + Windows pre-check
dbread upgrade --check       # dry-run: print current vs latest PyPI version
```

`dbread upgrade` wraps `uv tool install --reinstall "dbread[<tracked extras>]"`
so the drivers you added with `dbread add-extra` survive the upgrade (a plain
`uv tool upgrade dbread` would drop them). On Windows it pre-checks
`tasklist` for OTHER running `dbread.exe` instances and aborts (file-lock
risk) — use `--force-windows` to bypass.

This skill (`~/.claude/skills/dbread/SKILL.md`) auto-refreshes on the **next**
`dbread` invocation if the bundled version differs — no manual
`dbread install-skill --force` needed. Tell the user to restart Claude Code
afterwards so the new skill is loaded for the current session.

### Upgrade fails on Windows: `os error 32` / "being used by another process"

If `dbread upgrade` aborts with `dbread.exe is running`, another instance
(usually the Claude Code MCP server) holds the file open. Tell the user:

```powershell
# Quit Claude Code completely (not just minimize), then:
Get-Process dbread -ErrorAction SilentlyContinue | Stop-Process -Force
dbread upgrade
# Then reopen Claude Code.
```

Linux/macOS don't have this restriction (running binaries can be replaced).

## Don't do

- Don't call `query` before `describe_table` unless the user explicitly lists column names.
- Don't retry a query that failed with `sql_guard` — the guard is deterministic; it will fail again.
- Don't chain many small queries when a single JOIN/aggregate answers the question — respect rate limits.
- Don't inline raw user input as SQL values — pass `params` instead. It keeps
  quoting correct and keeps the literal out of the audit log.
- Don't assume a MongoDB collection has a consistent schema — `describe_table`
  and `profile_table` return **sampled** results; rare fields may be missing.
- Don't treat `profile_table` numbers as exact — they describe the sample, not
  the whole table. Say so when reporting them.
