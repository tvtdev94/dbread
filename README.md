<div align="center">

<img src="https://raw.githubusercontent.com/tvtdev94/dbread/master/docs/images/hero-banner.png" alt="dbread - Read-only DB MCP Proxy for AI" width="100%" />

# `dbread`

### Read-only database MCP proxy for AI — safe `SELECT` + MongoDB read access with 5-layer defense

[![PyPI](https://img.shields.io/pypi/v/dbread?color=3775a9&logo=pypi&logoColor=white)](https://pypi.org/project/dbread/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-1.0+-6e56cf?logo=anthropic&logoColor=white)](https://modelcontextprotocol.io/)
[![CI](https://github.com/tvtdev94/dbread/actions/workflows/ci.yml/badge.svg)](https://github.com/tvtdev94/dbread/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-539%20passing-22c55e)](#-testing)
[![Coverage](https://img.shields.io/badge/coverage-87%25-0891b2)](#-testing)
[![Built with uv](https://img.shields.io/badge/built%20with-uv-de5fe9)](https://docs.astral.sh/uv/)
[![License MIT](https://img.shields.io/badge/license-MIT-94a3b8)](LICENSE)

[**Why**](#-why) · [**Quickstart**](#-quickstart-2-minutes-no-clone-needed) · [**Add a connection**](#2c-add-your-real-connection-one-command) · [**CLI**](#-cli-commands) · [**Tools**](#-tools) · [**Security**](#%EF%B8%8F-security-model) · [**Update**](#-update--add-more-drivers) · [**Docs**](#-docs)

</div>

---

## 🤔 Why

Handing a raw database connection string to an AI is like handing a stranger your car keys. They *probably* won't crash it, but you wouldn't bet the car on it.

**dbread sits between your AI and your DBs** and enforces read-only access through **five independent layers** — if one layer has a bug, the next one still blocks you.

<div align="center">
<img src="https://raw.githubusercontent.com/tvtdev94/dbread/master/docs/images/layers-diagram.png" alt="5-layer defense in depth" width="100%" />
</div>

---

> ⚠️ **Security note — do not skip.** Layer 0 (a read-only DB user, [step 2b](#2b-create-a-read-only-db-user-when-pointing-at-a-real-db)) is the **only non-bypassable guarantee**. Layers 1–4 reduce blast radius and make attacks loud — they are **not substitutes**. If you point dbread at a DB where the configured user can write, a single sqlglot parser gap (past, present, or future) can let a write through. See [Known Limitations](#-known-limitations).

## ⚡ Quickstart (2 minutes, no clone needed)

### 1. Install as a tool

```bash
# From PyPI (recommended):
uv tool install "dbread[postgres]"          # extras: postgres, mysql, mssql, oracle, duckdb, clickhouse, mongo

# OR straight from GitHub (no PyPI needed):
uv tool install "git+https://github.com/tvtdev94/dbread[postgres]"
```

> 💡 Pick the extras you need now. To add more drivers later, use `dbread add-extra <name>` (preserves prior).

### 2. Scaffold config (one command)

```bash
dbread init
```

Creates `~/.dbread/config.yaml`, `~/.dbread/.env`, and `~/.dbread/sample.db` (a tiny read-only SQLite demo so everything works immediately). Prints the exact `claude mcp add` line to paste in step 4. Skip to step 4 if you only want the demo; otherwise edit `config.yaml` / `.env` first (step 3).

> 💡 **Claude Code users**: `dbread init` also installs a **skill** at `~/.claude/skills/dbread/SKILL.md` that teaches Claude the safe query workflow (discover → describe → query), SQL vs MongoDB routing, and how to recover from guard / rate-limit errors. Reinstall later with `dbread install-skill --force` after upgrading.

### 2b. Create a read-only DB user (when pointing at a real DB)

See [`docs/setup-db-readonly.md`](docs/setup-db-readonly.md) — copy-paste SQL/Mongo snippets for PostgreSQL / MySQL / MSSQL / Oracle / SQLite / DuckDB / ClickHouse / **MongoDB**, plus compat notes for CockroachDB · Timescale · Aurora · SingleStore · PlanetScale · Yugabyte · DocumentDB · CosmosDB.

### 2c. Add your real connection (one command)

Have a connection string from a tool, cloud console, or `.NET` / JDBC / ODBC config? Don't hand-edit YAML:

```bash
dbread add
```

Paste your connection string when prompted (input is hidden — safe for passwords). dbread:

1. **Auto-detects** the format — URI · JDBC · ADO.NET / C# · ODBC · MongoDB Atlas (`mongodb+srv://`) · MotherDuck · file path
2. **Converts** to the right SQLAlchemy URL with the correct `+driver` suffix and URL-escaped password
3. **Tests** with `SELECT 1` (or Mongo `ping`) — fail-fast before saving
4. **Writes** `~/.dbread/.env` (chmod `0600` on POSIX) + `~/.dbread/config.yaml` (preserves your comments)

Supports PostgreSQL · MySQL · MSSQL · Oracle · SQLite · DuckDB · ClickHouse · MongoDB.

```bash
# Common flags:
dbread add prod_pg                          # provide a name (otherwise prompted)
dbread add --no-test                        # skip the live connection test
dbread add --dialect-hint mssql             # force dialect when ambiguous
dbread add --manual --dialect-hint postgres # skip detection, paste a SQLAlchemy URL directly
dbread add --from-stdin < my-conn.txt       # script-friendly (non-interactive)
```

> 💡 **Auto-detect failed?** dbread offers a fallback menu — paste a SQLAlchemy URL manually, generate a
> copy-paste config template, or cancel. Same recovery if the live test fails (save anyway / edit & retry / cancel).

See [`docs/connection-string-formats.md`](docs/connection-string-formats.md) for all recognised formats and
URL templates per dialect, and [`docs/cli-reference.md`](docs/cli-reference.md) for the complete CLI reference.

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

  # Optional: MongoDB (requires `uv tool install "dbread[mongo]"`)
  # analytics_mongo:
  #   url_env: MONGO_URL
  #   dialect: mongodb
  #   rate_limit_per_min: 60
  #   statement_timeout_s: 30    # becomes maxTimeMS=30000 per command
  #   max_rows: 1000
  #   mongo:
  #     sample_size: 100         # docs sampled by describe_table (10-1000)

audit:
  path: ~/.dbread/audit.jsonl   # ~ expansion supported
  rotate_mb: 50                  # rotates current → .1 → .2 → .3 (oldest dropped)
  timezone: UTC                  # IANA name; default UTC
  redact_literals: false         # true → SQL literals become "?" in log (PII hardening)
  retention_days: 7              # auto-prune entries older than N days (null = off)
```

```
# ~/.dbread/.env
MYDB_URL=postgresql+psycopg2://ai_readonly:password@host:5432/mydb
# MONGO_URL=mongodb://ai_ro:password@host:27017/analytics?tls=true
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
<img src="https://raw.githubusercontent.com/tvtdev94/dbread/master/docs/images/architecture-diagram.png" alt="dbread architecture" width="100%" />
</div>

**Data flow for a `query` call:**

<div align="center">
<img src="https://raw.githubusercontent.com/tvtdev94/dbread/master/docs/images/query-flow.png" alt="dbread query data flow" width="100%" />
</div>

<details>
<summary><b>Mermaid source</b> (for contributors — re-render with <code>mmdc</code>)</summary>

```mermaid
sequenceDiagram
    autonumber
    participant AI as 🤖 AI Agent
    participant T as tools.query
    participant G as Guard<br/>(SQL · Mongo)
    participant R as RateLimiter
    participant D as Database
    participant A as 📜 Audit

    AI->>+T: query(connection, sql | command)
    Note over T: route by dialect

    T->>+G: validate()
    alt 🚫 write · JS-exec · blacklisted fn
        G-->>T: rejected(reason)
        T->>A: log(rejected)
        T-->>AI: ❌ guard_error
    else ✅ read-only
        G-->>-T: allowed + auto-inject LIMIT N

        T->>+R: acquire(connection)
        alt 🚫 per-conn OR global QPM hit
            R-->>T: denied(scope)
            T->>A: log(rate_limit)
            T-->>AI: ❌ rate_limit_exceeded
        else ✅ token granted
            R-->>-T: ok
            T->>+D: execute (statement_timeout)
            D-->>-T: rows
            T->>A: log(ok, rows, ms)
            T-->>AI: ✅ rows JSON
        end
    end
    deactivate T
```

Source: `docs/images/query-flow.mmd`. Regenerate with:
```bash
npx -p @mermaid-js/mermaid-cli mmdc \
  -i docs/images/query-flow.mmd \
  -o docs/images/query-flow.png \
  -c docs/images/mermaid-config.json \
  -b "#0f172a" -w 1600 -H 1200 --scale 2
```

</details>

---

## 🧰 Tools

| Tool | Purpose | Input |
|------|---------|-------|
| `list_connections` | Configured connections + dialects | — |
| `list_tables` | Tables in a connection | `connection`, `schema?` |
| `describe_table` | SQL: columns/types/PKs/indexes. Mongo: sampled field schema + indexes | `connection`, `table`, `schema?` |
| `query` | Run `SELECT`/`WITH`/`EXPLAIN`/`SHOW` (SQL) **or** Mongo `command` (find/count/distinct/aggregate). Auto-limited. Rate-limited. Audited. | `connection`, `sql` \| `command`, `max_rows?` |
| `explain` | Query execution plan | `connection`, `sql` \| `command` |

---

## 💻 CLI Commands

dbread is also a CLI for setup, troubleshooting, and audit analysis.

| Command | Purpose |
|---------|---------|
| `dbread` | Start the MCP stdio server (reads `DBREAD_CONFIG`) |
| `dbread init` | Scaffold `~/.dbread/{config.yaml,.env,sample.db}` + install Claude skill |
| `dbread add [name]` | **Interactive wizard** — paste any connection string, auto-detect & convert |
| `dbread add-extra <e1> ...` | Install additional driver extras **without losing existing ones** |
| `dbread list-extras` | Show tracked vs actually-importable extras |
| `dbread doctor` | Check `config.yaml` dialects against installed drivers; print fix command |
| `dbread audit [opts]` | Analyze `audit.jsonl` (`--since`, `--conn`, `--slow`, `--rejected`, `--tail`) |
| `dbread install-skill [--force]` | Install/refresh the Claude Code skill |
| `dbread --version` / `--help` | Self-explanatory |

Full reference: [`docs/cli-reference.md`](docs/cli-reference.md).

---

## 🛡️ Security Model

| Layer | Mechanism | What it rejects |
|:-:|---|---|
| **0** | DB user with `GRANT SELECT` only | **All writes — mandatory, non-bypassable** |
| **1** | `sqlglot` AST validation (SQL) · allowlist validator (Mongo) | **SQL:** `INSERT` · `UPDATE` · `DELETE` · `MERGE` · `CREATE` · `ALTER` · `DROP` · `TRUNCATE` · `GRANT` · `REVOKE` · multi-statement (`SELECT 1; DROP...`) · **PG CTE-DML trick** (`WITH d AS (DELETE...) SELECT...`) · time-based DoS (`pg_sleep*`, `sleep`, `benchmark`, MSSQL `WAITFOR DELAY/TIME`) · function blacklist (`pg_read_file`, `xp_cmdshell`, `load_file`, `dblink_exec`, ClickHouse `url`/`s3`/`remote`, DuckDB `read_csv`/`read_parquet`, …). **Mongo:** only `find`/`count`/`distinct`/`aggregate`; blocks `$out` · `$merge` · `$function` · `$accumulator` · `$where` · `mapReduce` · `$unionWith` · cross-DB `$lookup` · recursively walks `$facet`/`$lookup.pipeline`. |
| **2** | Rate limit + `statement_timeout` | Runaway loops · long-running queries |
| **3** | Auto-inject `LIMIT N` | Oversized result sets |
| **4** | JSONL audit log (`fsync` each write, 3-backup rotate, opt-in PII redact) | *(detection, not prevention — grep-friendly forensics)* |

> 💡 **Principle:** Never rely on a single layer. Layer 0 is the guarantee; Layers 1–4 make attacks loud and rare.

Full threat model: [`docs/security-threat-model.md`](docs/security-threat-model.md) (STRIDE analysis).

---

## ⚡ Overhead

dbread adds guard + limit-injection work on every `query`. Rough p95 per call, measured in-process (no DB round-trip):

| Workload | guard.validate | guard.inject_limit | total overhead |
|----------|---------------:|-------------------:|---------------:|
| `SELECT 1` | ~0.17 ms | ~0.44 ms | **~0.6 ms** |
| Realistic WHERE + ORDER BY | ~0.65 ms | ~1.28 ms | **~1.9 ms** |
| 5-CTE 10-table join | ~3.1 ms | ~4.8 ms | **~7.9 ms** |

Rate-limit `acquire`: ~1 µs. **Run `uv run python scripts/benchmark_overhead.py` on your box.** Full methodology: [`docs/benchmarks.md`](docs/benchmarks.md).

---

## 📋 Example Prompts

```
💬 "List connections in dbread."
💬 "Describe the schema of the orders table in analytics_prod."
💬 "Top 10 customers by lifetime value — use dbread."
💬 "Run EXPLAIN on: SELECT ... ORDER BY created_at"
💬 "Count orders by status in analytics_mongo (use aggregate)."
💬 '{"find": "users", "filter": {"status": "active"}}'   (Mongo command form)
```

```
💬 "Update user 1 to 'hacked'."
   → ❌ sql_guard: node_rejected: Update

💬 "WITH d AS (DELETE FROM users RETURNING *) SELECT * FROM d"
   → ❌ sql_guard: node_rejected: Delete   (PG CTE-DML blocked)

💬 "SELECT 1; DROP TABLE users;"
   → ❌ sql_guard: multi_statement_not_allowed

💬 '{"aggregate": "users", "pipeline": [{"$out": "leak"}]}'
   → ❌ mongo_guard: blocked_operator: $out
```

---

## 📜 Audit Log

Every call lands in `audit.jsonl` — one JSON per line, append-only, `fsync`'d on each write (survives `kill -9`), auto-rotated at 50 MB through a 3-backup chain (`.1` → `.2` → `.3`).

```jsonc
{"ts":"2026-04-22T12:30:12+00:00","conn":"analytics","sql":"SELECT * FROM users LIMIT 100","rows":100,"ms":42,"status":"ok"}
{"ts":"2026-04-22T12:30:15+00:00","conn":"analytics","sql":"DELETE FROM users","rows":0,"ms":0,"status":"rejected","reason":"node_rejected: Delete"}
```

Default timezone is **UTC**; override with `audit.timezone: Asia/Bangkok` (IANA). Enable `audit.redact_literals: true` to rewrite SQL literals to `?` before logging — handy when prompts may contain PII.

Set `audit.retention_days: N` to auto-prune entries older than N days. Runs once at startup then at most once per hour on subsequent writes — covers both current file and rotated backups (`.1`, `.2`, `.3`). Malformed lines are kept (fail-safe). Leave unset to keep only size-based rotation (max ~4 × `rotate_mb`).

```bash
jq 'select(.status=="rejected")' audit.jsonl     # just rejections
jq 'select(.ms > 1000)' audit.jsonl              # slow queries
jq -s 'group_by(.status)|map({s:.[0].status,n:length})' audit.jsonl   # counts
```

No `jq`? Use the built-in analyzer:

```bash
dbread audit                     # summary: counts, top slow, top rejected
dbread audit --since 1h          # last hour only
dbread audit --conn analytics    # filter by connection
dbread audit --slow 1000         # queries >= 1000 ms
dbread audit --rejected          # only rejections, grouped by reason
dbread audit --tail              # follow new entries (like tail -f)
```

Rotated backups (`.1` · `.2` · `.3`) are aggregated automatically.

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

  local_duckdb:
    url: duckdb:///./analytics.duckdb?access_mode=read_only
    dialect: duckdb
    rate_limit_per_min: 200
    statement_timeout_s: 30
    max_rows: 5000

  clickhouse_prod:
    url_env: CLICKHOUSE_URL            # clickhouse+http://readonly:pw@host:8123/db
    dialect: clickhouse
    rate_limit_per_min: 60
    statement_timeout_s: 30
    max_rows: 1000

audit:
  path: ~/.dbread/audit.jsonl         # ~ expansion supported
  rotate_mb: 50                        # rotate chain: current → .1 → .2 → .3
  timezone: UTC                        # IANA; default UTC
  redact_literals: false               # true → SQL literals → "?"
  retention_days: 7                    # auto-prune entries older than N days (null = off)
```

Supported dialects: `postgres` · `mysql` · `mssql` · `sqlite` · `oracle` · `duckdb` · `clickhouse` · `mongodb`.

Compat (no new dialect): CockroachDB, TimescaleDB, Aurora PG (use `postgres`) · Aurora MySQL, SingleStore, PlanetScale (use `mysql`). See [`docs/setup-db-readonly.md`](docs/setup-db-readonly.md#compatible-databases-no-new-dialect-needed).

---

## 🧪 Testing

```bash
uv sync --extra dev
uv run pytest                          # 539 passing
uv run pytest --cov=dbread             # coverage report (87% overall)
uv run ruff check src/                 # lint

# Integration tests with real PG + MySQL + ClickHouse + MongoDB (needs Docker):
cd tests/integration && docker compose up -d
uv run pytest tests/integration/ -v
```

- **520+ unit tests** cover config, connections, audit (fsync/tz/redact/rotate), SQL guard (**57 evasion cases incl. WAITFOR & sleep variants**), Mongo guard (**22 adversarial cases — $out/$merge smuggling, JS exec, cross-DB $lookup, deep nesting**), rate limiter, tools, **plus the v0.7 connection-string parsers** (84 tests across 6 format families × 8 dialects), **converter** (54 tests), **wizard + writers** (47 tests), **extras tracking** (36 tests), **CLI** (22 tests).
- **4 subprocess smoke tests** drive `server.py` via real stdio JSON-RPC.
- **4 SQLite + 4 DuckDB E2E tests** always run (no Docker).
- **PG + MySQL + ClickHouse + MongoDB E2E tests** skip gracefully without Docker.
- CI runs on **GitHub Actions matrix**: Python 3.11/3.12 × Ubuntu/Windows + a dedicated `extras-install-order` job that proves `dbread add-extra` preserves prior drivers.

---

## ⚠️ Known Limitations

Honesty pass — what dbread does *not* do:

- **sqlglot is best-effort, dialect-dependent.** Coverage is strong for Postgres / MySQL / SQLite; medium for MSSQL / Oracle / ClickHouse / DuckDB. See the [dialect coverage table](docs/security-threat-model.md#dialect-coverage-layer-1). Function blacklists are deny-lists; new dialect features arrive between releases.
- **Rate limit is single-process, in-memory.** Multiple dbread processes (multi-user install) don't share buckets. `global_rate_limit_per_min` caps one process only.
- **Audit is reactive, not preventive.** JSONL + `dbread audit` help you notice; they don't block.
- **No query cost estimator.** Layer 2 has `statement_timeout` and `LIMIT N`, but an expensive index-less scan that finishes in time still runs.
- **Pre-1.0 project.** Real-world adversarial testing accumulates over time. Treat dbread as one layer of defense, not the whole perimeter.
- **MongoDB guard is new (v0.4).** Allowlist-based, less battle-tested than sqlglot. Adversarial suite covers the known write-stage / JS-exec evasions; report new ones.
- **Mongo schema is sampled, not authoritative** (default 100 docs). Rare fields may be missed — bump `mongo.sample_size` (max 1000) if needed.
- **No Atlas Search / `$search` / `$vectorSearch` support.** Deferred to v0.5+.

---

## 🔄 Update / add more drivers

```bash
# Upgrade to latest:
uv tool upgrade dbread

# Add a new driver later WITHOUT losing existing ones:
dbread add-extra mongo
# (under the hood: uv tool install --force "dbread[postgres,mysql,mongo,...]")

# Check what's installed and what your config needs:
dbread doctor
dbread list-extras
```

> ⚠️ **Why `add-extra` instead of `uv tool install dbread[mongo]`?** Because `uv tool install` always recreates the
> tool environment, dropping previously-installed extras. `dbread add-extra` tracks the union of all installed extras
> and reinstalls correctly so you never lose drivers you already had.

Working from a git checkout (source install)? Run `bash scripts/dev-install.sh` (or the `.ps1` variant) — see the
[Development](#%EF%B8%8F-development) section.

---

## 📚 Docs

| Document | What's in it |
|----------|--------------|
| [`docs/setup-db-readonly.md`](docs/setup-db-readonly.md) | **Copy-paste SQL / Mongo** for Layer 0 read-only user on PG / MySQL / MSSQL / Oracle / SQLite / DuckDB / ClickHouse / MongoDB |
| [`docs/cli-reference.md`](docs/cli-reference.md) | Complete CLI reference for all `dbread` commands with examples and exit codes |
| [`docs/connection-string-formats.md`](docs/connection-string-formats.md) | Every format `dbread add` recognises + SQLAlchemy URL templates per dialect + fallback options |
| [`docs/architecture.md`](docs/architecture.md) | Component diagram · 5-layer details · data flow · design decisions |
| [`docs/security-threat-model.md`](docs/security-threat-model.md) | Full STRIDE analysis · residual risks · response plan |
| [`docs/benchmarks.md`](docs/benchmarks.md) | Overhead methodology + per-workload p95 numbers |
| [`docs/manual-smoke-test.md`](docs/manual-smoke-test.md) | Step-by-step checklist for verifying integration with Claude Code |

---

## 🧱 Project Layout

```
src/dbread/
├── server.py            # MCP stdio entry — registers 5 tools, dispatches to handlers
├── tools.py             # SQL tool handlers (guard → limit → rate → exec → audit)
├── sql_guard.py         # sqlglot AST validator + LIMIT injection
├── rate_limiter.py      # thread-safe token bucket per connection + global cap
├── connections.py       # SQLAlchemy engine manager (lazy, per-dialect)
├── config.py            # pydantic Settings (YAML + env)
├── audit.py             # append-only JSONL with fsync + size rotation + redaction
├── audit_cli.py         # `dbread audit` analyzer (since/conn/slow/rejected/tail)
├── cli.py               # CLI dispatcher: init, add, add-extra, list-extras, doctor, audit, ...
├── mongo/               # MongoDB stack
│   ├── client.py        # MongoClient manager (one per connection name)
│   ├── guard.py         # allowlist validator + limit injection for commands
│   ├── schema.py        # sample-based schema inference
│   └── tools.py         # Mongo tool handlers (list/describe/query/explain)
├── extras/              # NEW v0.7 — driver-extra tracking
│   ├── manager.py       # state file (~/.dbread/installed_extras.json) + find_spec scan
│   └── installer.py     # subprocess wrapper: `uv tool install --force "dbread[<union>]"`
└── connstr/             # NEW v0.7 — `dbread add` connection-string wizard
    ├── types.py         # ParsedConn dataclass + UnsupportedConnString / UnknownFormat
    ├── detector.py      # priority-ordered format dispatch
    ├── converter.py     # ParsedConn → SQLAlchemy URL (URL.create + Mongo hand-build)
    ├── wizard.py        # interactive 10-step flow (paste → detect → test → write)
    ├── writers.py       # comment-preserving .env / config.yaml writers
    ├── _manual_entry.py # fallback: manual URL entry + template generator
    └── parsers/         # one per format family
        ├── uri.py       # postgresql://, mysql://, mongodb://, mongodb+srv://, ...
        ├── jdbc.py      # jdbc:postgresql://, jdbc:oracle:thin:@..., ...
        ├── adonet.py    # Server=...;Database=...;User Id=...;Password=...; (C# / .NET)
        ├── odbc.py      # Driver={ODBC Driver 17 for SQL Server};Server=...;...
        ├── cloud.py     # mongodb+srv://, MotherDuck md:, *.clickhouse.cloud
        └── filepath.py  # *.db / *.sqlite / *.duckdb / :memory:
```

Every core runtime module stays **small and single-purpose** — most files sit
under 200 LOC so the whole stack is readable end-to-end in a single sitting.

---

## 🛠️ Development

Working from source (no PyPI release yet, or iterating on a patch)? Use the
dev-install scripts — they reinstall the `uv tool` **with** a full wheel
rebuild even when the version hasn't changed (`uv tool install --force`
alone does NOT re-copy source when the version is unchanged).

```bash
# Bash (macOS, Linux, Git-Bash on Windows)
bash scripts/dev-install.sh            # installs [mongo] extras by default
bash scripts/dev-install.sh "mongo,postgres,mysql"
```

```powershell
# PowerShell
.\scripts\dev-install.ps1
.\scripts\dev-install.ps1 -Extras "mongo,postgres,mysql"
```

Then in Claude Code: `/mcp` → pick `dbread` → **Reconnect** so the new tool
list is fetched.

---

## 🙏 Credits

Built with [`mcp`](https://modelcontextprotocol.io/) · [`sqlglot`](https://sqlglot.com/) · [`SQLAlchemy 2.x`](https://www.sqlalchemy.org/) · [`pydantic`](https://docs.pydantic.dev/) · [`uv`](https://docs.astral.sh/uv/).

---

<div align="center">
<sub>Made with ❤️ for developers who want AI productivity <strong>without</strong> giving up database safety.</sub>
</div>
