# Security Threat Model (STRIDE)

## Scope

Single-process MCP server on a developer workstation. Client = Claude Code (trusted, but prompt-injection prone). Database = external.

## Assets

- Data in the database (primary).
- Credentials in `.env` / `config.yaml`.
- Audit log (forensic value).

## Trust Boundaries

- Client ↔ MCP server (stdio) — same OS user.
- MCP server ↔ DB (network) — DB enforces Layer 0.

## STRIDE Table

| Threat | Vector | Mitigation (Layer) | Residual Risk |
|--------|--------|---------------------|---------------|
| **S**poofing: attacker impersonates the dbread binary | Malicious binary on PATH | Install from trusted source; `uv` lockfile pins deps | Low |
| **T**ampering: modify SQL mid-flight | N/A in local stdio | — | Negligible |
| **T**ampering: AI crafts SQL to bypass guard | Obscure syntax, CTE-DML, comment evasion | Layer 1 AST walk + Layer 0 (user cannot write) | Low — Layer 0 guarantees |
| **T**ampering: bypass dbread via direct DB access | User shares credentials outside MCP | Docs: the RO user is for dbread only | Out of scope |
| **R**epudiation: "I didn't run that query" | — | Layer 4 audit JSONL with timestamp + SQL | Low |
| **I**nformation disclosure: credentials leak | Committing `.env` / `config.yaml` | `.gitignore` + `url_env` env-var pattern | Low if docs followed |
| **I**nformation disclosure: audit contains PII | Raw SQL logged | Layer 4: optional `audit.redact_literals` rewrites SQL literals to `?` via sqlglot; 3-backup rotation | Medium — documented |
| **I**nformation disclosure: audit record lost on crash | Power loss / `kill -9` mid-write | Layer 4: `fsync()` after every record | Low |
| **I**nformation disclosure: credentials plaintext on wire | URL without TLS | Warn on `get_engine` if `sslmode=`/`ssl=`/`encrypt=` absent (PG/MySQL/MSSQL) | Medium — documented |
| **I**nformation disclosure: sensitive tables readable | Over-broad GRANT SELECT | Docs: grant minimum tables/schemas | User-config dependent |
| **D**enial of Service: runaway query | AI loops large queries | Layer 2 rate limit + DB `statement_timeout` + LIMIT injection | Low |
| **D**enial of Service: audit fills disk | Unbounded log | Layer 4 rotation at 50 MB (1 backup = 100 MB cap) | Low |
| **E**levation: side-effect function (`pg_read_file`, `xp_cmdshell`) | SELECT wrapping dangerous function | Layer 1 function blacklist + Layer 0 (no EXECUTE on superuser fns) | Low |
| **D**enial of Service: time-based (`pg_sleep`, `dbms_lock.sleep`, `WAITFOR DELAY`) | Long-sleeping SELECT | Layer 1 blacklist (function + `WAITFOR` regex) + Layer 2 timeout | Low |
| **E**levation: DuckDB/ClickHouse external table functions | `read_csv`, `url`, `s3`, `remote`, `mysql_table` | Layer 1 blacklist (anonymous + typed AST class names) | Low |
| **E**levation: PG CTE-DML trick (`WITH d AS (DELETE...) SELECT...`) | `RETURNING *` from CTE | Layer 1 walks `With.expressions` + Layer 0 | Low |
| **E**levation: multi-statement injection (`SELECT 1; DROP ...`) | Driver allowing multi-stmt | Layer 1 rejects `len(stmts) > 1`; driver flag where available | Low |
| **E**levation: unknown statement type (VACUUM, SET, CALL) | `exp.Command` catch-all | Layer 1 rejects top-level `Command` unless in ALLOW list + Layer 0 | Low |

## Assumption Log

- User follows [setup-db-readonly.md](setup-db-readonly.md) — **critical**.
- Workstation not compromised (dbread is not a network trust boundary).
- sqlglot keeps pace with dialect edge cases — version is pinned; re-audit on upgrade.

## Response Plan

- Layer 1 bypass discovered → Layer 0 prevents damage → patch guard → release.
- Audit log shows unusual pattern → `jq 'select(.status=="rejected")'` → correlate with the originating AI session.
