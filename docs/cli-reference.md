# dbread CLI Reference

Complete reference for all `dbread` commands. See `dbread --help` for a quick overview.

## dbread (no args) — Start MCP server

Starts the dbread MCP stdio server. This is the default mode when you invoke `dbread` with no arguments.

### Usage

```bash
dbread
```

The MCP server reads `DBREAD_CONFIG` environment variable (path to `config.yaml`) and registers the following tools:
- `list_connections`
- `list_tables`
- `describe_table`
- `query`
- `explain`

Runs indefinitely; connect via Claude Code with `claude mcp add --scope user dbread -- dbread`.

### Exit codes
- `0` — server running (exits only on SIGTERM/SIGINT)
- `1` — failed to load config

---

## dbread init — Scaffold configuration

Creates a working default setup in `~/.dbread/` with a sample SQLite database.

### Usage

```bash
dbread init
```

### What it does

1. Creates `~/.dbread/config.yaml` (gitignored — safe for real values)
2. Creates `~/.dbread/.env` (holds `url_env` credentials)
3. Creates `~/.dbread/sample.db` (read-only SQLite demo with 3 sample rows)
4. Prints the exact `claude mcp add` command to register with Claude Code
5. Auto-installs the Claude Code skill at `~/.claude/skills/dbread/SKILL.md` (if `~/.claude` exists)

### Example session

```bash
$ dbread init
created  /home/user/.dbread/config.yaml
created  /home/user/.dbread/.env
created  /home/user/.dbread/sample.db
skipped  /home/user/.dbread/config.yaml (already exists)

Register with Claude Code:
  claude mcp add --scope user dbread \
    --env DBREAD_CONFIG=/home/user/.dbread/config.yaml \
    -- dbread
```

### Exit codes
- `0` — success (files created or skipped if already present)

### Notes

- Idempotent: safe to run multiple times (skips existing files).
- The sample database contains a `greetings` table with 3 rows for testing.
- Edit `~/.dbread/config.yaml` and `~/.dbread/.env` to add real connections.

---

## dbread install-skill [--force] — Install Claude Code skill

Installs or reinstalls the dbread skill that teaches Claude the query workflow and error-handling patterns.

### Usage

```bash
dbread install-skill         # install if not present
dbread install-skill --force # reinstall / overwrite
```

### What it does

1. Checks for `~/.claude/` (Claude Code installation)
2. Creates `~/.claude/skills/dbread/` directory
3. Writes `SKILL.md` with golden workflow, tool reference, error table, and troubleshooting

### Example session

```bash
$ dbread install-skill
created  /home/user/.claude/skills/dbread/SKILL.md

$ dbread install-skill --force
updated  /home/user/.claude/skills/dbread/SKILL.md
```

### Exit codes
- `0` — success or skipped (Claude Code not installed)
- `1` — permission denied writing to `~/.claude/`

### Notes

- Runs automatically during `dbread init` if `~/.claude` exists.
- Re-run after upgrading dbread to pick up workflow improvements.
- Reinstall with `--force` to overwrite old version.

---

## dbread add [name] [options] — Add a connection interactively

Wizard that accepts any connection-string format and writes both `config.yaml` and `.env`.

### Usage

```bash
dbread add                     # prompt for name + connection string
dbread add mydb                # skip name prompt, only ask for string
dbread add mydb --no-test      # skip connection test
dbread add mydb --from-stdin   # read connection string from stdin (for scripts)
dbread add mydb --dialect-hint postgres  # hint if auto-detect is wrong
```

### What it does

1. Prompts for connection name (skipped if provided as arg)
2. Prompts for connection string (input hidden — safe for passwords)
3. Auto-detects format: URI, JDBC, ADO.NET, ODBC, MongoDB Atlas, file path
4. Converts to SQLAlchemy URL
5. Tests connection (unless `--no-test`)
6. Appends connection to `~/.dbread/config.yaml`
7. Writes credential to `~/.dbread/.env`
8. Confirms success

### Example session

```bash
$ dbread add
Connection name: analytics_prod
Connection string (hidden): postgresql+psycopg2://ai_ro:secretpw@prod.db.com:5432/analytics
Testing connection...
✓ OK — connection test passed

Added to config.yaml:
  analytics_prod:
    url_env: ANALYTICS_PROD_URL
    dialect: postgres
    rate_limit_per_min: 60
    statement_timeout_s: 30
    max_rows: 1000

Credential written to .env:
  ANALYTICS_PROD_URL=postgresql+psycopg2://ai_ro:secretpw@prod.db.com:5432/analytics

Register with Claude Code:
  claude mcp add --scope user dbread \
    --env DBREAD_CONFIG=/home/user/.dbread/config.yaml \
    -- dbread
```

### Options

| Flag | Description |
|------|-------------|
| `--no-test` | Skip connection test (for offline / manual validation) |
| `--from-stdin` | Read connection string from stdin instead of prompt |
| `--dialect-hint <dialect>` | Hint for auto-detect (postgres, mysql, mssql, oracle, sqlite, duckdb, clickhouse, mongodb) |

### Supported formats

See [`docs/connection-string-formats.md`](connection-string-formats.md) for the complete list of recognized formats.

### Exit codes
- `0` — connection added successfully
- `1` — user cancelled (Ctrl+C)
- `2` — invalid args or unrecognized format
- `3` — connection test failed or config write error

---

## dbread add-extra <e1> [e2] ... — Install driver extras

Installs additional database drivers (extras) while preserving all previously-installed ones.

### Usage

```bash
dbread add-extra mongo                  # add MongoDB support
dbread add-extra mysql oracle           # add multiple at once
dbread add-extra postgres mysql mssql   # install multiple
```

### What it does

1. Loads current extras from state file (or bootstraps if missing)
2. Merges new extras with existing ones (union)
3. Runs `uv tool install --force "dbread[<union>]"`
4. Saves updated state for next install

### Example session

```bash
$ dbread add-extra mongo
OK  extras now: postgres, mysql, mongo

$ dbread add-extra oracle
OK  extras now: postgres, mysql, mongo, oracle
```

### Why not just `uv tool install dbread[mongo]`?

Because `uv tool install` always recreates the tool environment from scratch, dropping previously-installed extras.
`dbread add-extra` tracks the union of all extras and reinstalls correctly.

### Available extras

`postgres`, `mysql`, `mssql`, `oracle`, `duckdb`, `clickhouse`, `mongo`

### Exit codes
- `0` — extras installed or already present
- `2` — unknown extra name
- `3` — install failed (network, permission, or environment issue)

---

## dbread list-extras — Show driver install matrix

Displays which extras are tracked (state file), importable (available in code), and actually installed (in environment).

### Usage

```bash
dbread list-extras
```

### Example output

```
Install method: uv tool
State file:     present

EXTRA        TRACKED    IMPORTABLE
clickhouse   no         yes
duckdb       yes        yes
mongo        yes        yes
mssql        no         yes
mysql        yes        yes
oracle       no         yes
postgres     yes        yes
```

| Column | Meaning |
|--------|---------|
| `TRACKED` | Recorded in state file (previously installed via `add-extra`) |
| `IMPORTABLE` | Recognized by dbread code (can be installed) |
| Actual importable status | Verified by attempting `import` in current environment |

### Exit codes
- `0` — matrix displayed

---

## dbread doctor — Check config vs installed drivers

Analyzes `config.yaml` and warns if any configured dialects are missing their drivers.

### Usage

```bash
dbread doctor
```

### Example output (all OK)

```
Config:    /home/user/.dbread/config.yaml
Dialects:  postgres, mysql
Extras needed:    postgres, mysql
Extras installed: mysql, postgres
OK  all dialects in config have drivers installed.
```

### Example output (missing driver)

```
Config:    /home/user/.dbread/config.yaml
Dialects:  postgres, mysql, mongo
Extras needed:    postgres, mysql, mongo
Extras installed: mysql, postgres

MISSING extras: mongo

Fix:
  dbread add-extra mongo
  # or manually:
  uv tool install --force "dbread[postgres,mysql,mongo]"
```

### Exit codes
- `0` — all configured dialects have drivers installed
- `3` — one or more drivers missing; suggestions printed

### Use cases

- After editing `config.yaml`: run `dbread doctor` to catch missing drivers
- After upgrading dbread: run to verify all prior extras are still present
- Troubleshooting module not found errors: run `dbread doctor` to diagnose

---

## dbread audit [options] — Analyze audit log

Analyzer for the `audit.jsonl` log file. Shows summaries, filters, and statistics.

### Usage

```bash
dbread audit                    # summary: counts, top slow, top rejected
dbread audit --since 1h         # last hour only
dbread audit --since 24h        # last 24 hours
dbread audit --conn analytics   # filter by connection name
dbread audit --slow 1000        # queries >= 1000 ms
dbread audit --rejected         # only rejections, grouped by reason
dbread audit --tail             # follow new entries (like tail -f)
```

### Options

| Flag | Description |
|------|-------------|
| `--since <duration>` | Show entries since (e.g., `1h`, `24h`, `7d`, `30d`) |
| `--conn <name>` | Filter by connection name |
| `--slow <ms>` | Show queries that took >= N milliseconds |
| `--rejected` | Show only rejected queries, grouped by reason |
| `--tail` | Follow new entries as they append (Ctrl+C to exit) |

### Example session

```bash
$ dbread audit --rejected
Rejections (last 100):
  node_rejected: Delete      2
  function_blacklisted       1
  sql_guard: multi_statement 3

$ dbread audit --slow 5000
Slow queries (>= 5000 ms):
  2026-04-22T14:30:22Z  analytics  SELECT ... (8123 ms)
  2026-04-22T14:35:10Z  old_prod   SELECT ... (6450 ms)
```

### Exit codes
- `0` — analysis complete
- `3` — audit file not found or corrupted

### Notes

- Rotated backups (`.1`, `.2`, `.3`) are automatically aggregated.
- Malformed lines are skipped (fail-safe).

---

## dbread --version — Print version

Shows the installed dbread version.

### Usage

```bash
dbread --version
```

### Example output

```
dbread 0.7.0
```

### Exit codes
- `0` — version printed

---

## dbread --help — Print help

Shows a concise command summary.

### Usage

```bash
dbread --help
```

### Example output

```
dbread - read-only database MCP proxy for AI.

USAGE:
  dbread                         start the MCP stdio server (expects DBREAD_CONFIG)
  dbread init                    scaffold ~/.dbread/ + install Claude Code skill
  dbread install-skill [--force] install / reinstall ~/.claude/skills/dbread/SKILL.md
  dbread audit [opts]            analyze audit.jsonl (--since, --conn, --slow, --rejected, --tail)
  dbread add [name] [opts]       interactively add a new connection from a connection-string
                                 opts: --from-stdin, --no-test, --dialect-hint <pg|mysql|...>
  dbread add-extra <e1> ...      install additional driver extras (preserves prior)
  dbread list-extras             show tracked vs importable extras
  dbread doctor                  check config.yaml dialects vs installed drivers
  dbread --version               print version
  dbread --help                  print this help
```

### Exit codes
- `0` — help printed

---

## Environment variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `DBREAD_CONFIG` | Path to `config.yaml` | `/home/user/.dbread/config.yaml` |

Set `DBREAD_CONFIG` before running `dbread` (no args) to start the MCP server. Passed to Claude Code via `claude mcp add --env`.

Example:

```bash
export DBREAD_CONFIG=~/.dbread/config.yaml
dbread
```

Or in Claude Code:

```bash
claude mcp add --scope user dbread \
  --env DBREAD_CONFIG=~/.dbread/config.yaml \
  -- dbread
```
