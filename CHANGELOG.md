# Changelog

All notable changes to this project are documented here. This project adheres to
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] - 2026-04-22

Onboarding UX patch.

### Added

- **`dbread init` subcommand** — scaffolds `~/.dbread/{config.yaml, .env, sample.db}`
  in one step. `sample.db` ships with a tiny demo table so the MCP client
  works immediately after registration. Idempotent (skips existing files).
- **`dbread --version` / `--help`** — basic CLI affordances.

### Fixed

- Quickstart flow no longer requires the user to hand-edit paths or copy
  config templates. SQLite URL in the generated config uses an absolute
  path (SQLite URI does not expand `~`).

## [0.2.0] - 2026-04-22

Production polish — CI, async safety, audit hardening, and two new dialects.

### Added

- **CI matrix** (GitHub Actions): lint + tests on Python 3.11/3.12 across
  Ubuntu and Windows, with subprocess coverage of `server.py` (≥85% overall).
- **Subprocess smoke test** for the MCP server (initialize/list/call flow over
  stdio) — closes the 0% coverage gap on `server.py`.
- **DuckDB dialect** — install via `uv tool install "dbread[duckdb]"`.
  File-based; enforced read-only via `access_mode=read_only` URL.
- **ClickHouse dialect** — install via `uv tool install "dbread[clickhouse]"`.
  `readonly=1` + `max_execution_time` set via connect args as Layer-0 backup.
- **Compat DB docs**: CockroachDB, TimescaleDB, Aurora PG/MySQL, SingleStore,
  PlanetScale, YugabyteDB — reuse `postgres` / `mysql` dialects.
- **Opt-in PII redaction** (`audit.redact_literals: true`) — rewrites SQL
  literals to `?` via sqlglot before audit log.
- **Configurable audit timezone** (`audit.timezone`, default `UTC`, IANA name).
- **3-backup rotation chain** for `audit.jsonl` (`.1` → `.2` → `.3`).

### Changed

- **Tool handlers are now offloaded to a thread pool** (`asyncio.to_thread`) —
  a slow DB call no longer blocks the MCP event loop.
- **Audit writes are now `fsync`'d** — records survive `kill -9` / power loss.
- **Config `audit.path` supports `~`** expansion (`~/.dbread/audit.jsonl`).
- **Config file path** passed via `DBREAD_CONFIG` also supports `~` expansion.

### Security

- **SQL guard blacklist expanded**:
  - PostgreSQL: `pg_sleep`, `pg_sleep_for`, `pg_sleep_until` (time-based DoS).
  - MSSQL: `WAITFOR DELAY/TIME` rejected via pre-parse regex (reason
    `command_rejected: WAITFOR`).
  - Oracle: `dbms_lock.sleep`, `dbms_session.sleep` (caught by generic `sleep`).
  - ClickHouse external table functions: `url`, `s3`, `hdfs`, `remote`,
    `remote_secure`, `mysql_table`, `postgresql_table`, `mongodb`.
  - DuckDB external file readers: `read_csv`, `read_csv_auto`, `read_parquet`,
    `read_json`, `read_json_auto`, `read_ndjson` (matched by typed AST class
    name too).
- **TLS warning** emitted by `ConnectionManager.get_engine` when a PG/MySQL/MSSQL
  URL has no TLS hint (`sslmode=` / `ssl=` / `encrypt=`); local dev is not
  blocked, only warned.

### Deferred

- Cloud DW (BigQuery, Snowflake, Redshift) → v0.3.
- Metrics exporter, hypothesis fuzzing → v0.3.
- MongoDB support → v0.4 (new tool schema).

## [0.1.1] - Initial release

Baseline: 5-layer defense, 5 dialects (postgres/mysql/mssql/sqlite/oracle),
MCP stdio server, audit JSONL.
