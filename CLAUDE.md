# CLAUDE.md — Project conventions for AI agents

This file primes AI coding agents (Claude Code, Cursor, etc.) on
**non-obvious project conventions** for `dbread`. Read before touching code.

## Quick orientation

- **What it is:** Read-only database MCP proxy — 5-layer guard for safe SQL `SELECT` + MongoDB read access. See `README.md` for the full story.
- **Stack:** Python 3.11+, `mcp`, `sqlglot`, `SQLAlchemy 2.x`, `pydantic`, `pyyaml`. Build/install via `uv`.
- **Module size budget:** keep individual `.py` files under ~200 LOC. Split helpers when they grow.
- **Naming:** snake_case for Python modules (matches existing convention). Kebab-case for shell/yaml/markdown.
- **No new runtime deps** without explicit user approval. Dev deps under `[dev]` extra are fine.
- **Cross-platform:** Windows + macOS + Linux. Use `pathlib.Path`, gate POSIX-only calls (`os.chmod`) behind `sys.platform != "win32"`.

## Release process — FULLY AUTOMATED

**Do NOT manually run `twine upload` or `uv publish`.** This project has end-to-end automated PyPI publishing via GitHub Actions (set up 2026-04-26).

To ship a new version:

```bash
# 1. Bump version in BOTH places (workflow rejects mismatch)
sed -i 's/version = "0.7.4"/version = "0.7.5"/' pyproject.toml
sed -i 's/SERVER_VERSION = "0.7.4"/SERVER_VERSION = "0.7.5"/' src/dbread/server.py

# 2. Commit + tag + push
git commit -am "fix: v0.7.5 - <change>"
git tag -a v0.7.5 -m "v0.7.5"
git push --tags

# 3. Workflow auto-runs: verify -> build -> approval gate -> publish
#    Approve via GitHub UI, OR ask the AI to auto-approve via gh api.
```

**Full details:** [`docs/release-process.md`](docs/release-process.md) — workflow internals, safety gates, troubleshooting, how to auto-approve programmatically.

## Skill auto-refresh

When users run any `dbread` command after `uv tool upgrade dbread`, the bundled Claude Code skill at `~/.claude/skills/dbread/SKILL.md` is silently refreshed if the bundled version differs. Implementation: `auto_refresh_skill()` in `src/dbread/cli.py`, called from `server.py:main()`. Don't add another refresh trigger.

## Key directories

```
src/dbread/
├── server.py            # MCP stdio entry + CLI dispatcher (subcommand routing)
├── cli.py               # CLI commands: init, add, add-extra, list-extras, doctor, ...
├── connstr/             # `dbread add` wizard — paste connection string, auto-detect, write config
├── extras/              # Driver-extra tracking (state file + uv tool install --force union)
├── mongo/               # MongoDB tool handlers + guard
└── ...                  # SQL guard, audit, rate limiter, config (pydantic)
```

```
docs/
├── release-process.md   # ← THIS PROJECT IS AUTO-PUBLISH; read this first when releasing
├── cli-reference.md
├── connection-string-formats.md
├── architecture.md
├── security-threat-model.md
└── ...
```

## Testing conventions

- `pytest -m "not integration"` — unit tests only (Docker-free, runs everywhere)
- `pytest tests/integration/` — needs Docker for PG/MySQL/CH/Mongo
- `pytest --cov=dbread --cov-fail-under=85` — coverage gate
- `ruff check src/` — lint, must pass

Tests on dev box without `[mongo]` extra installed will show 4 failures + 10 errors in `test_mongo_client.py` and `tests/integration/test_e2e_mongo.py` — these are expected when pymongo + Docker absent. CI installs `[dev,mongo]` so it doesn't hit them.

## Things that have already been considered (don't redo)

- ✅ Switched from manual to auto-publish (2026-04-26)
- ✅ Skill auto-refresh on `dbread` invocation (v0.7.2+)
- ✅ Connection-string wizard with 6 format detectors + fallback (v0.7.0)
- ✅ Extras tracking via state file (no more lost drivers on incremental install) (v0.7.0)
- ✅ `python -m dbread` invocation support (v0.7.4)
- ✅ Connection name validation regex
- ✅ `.env` chmod 0600 on POSIX
- ✅ Wizard honors DBREAD_CONFIG env var
- ✅ Query-string secret masking (motherduck_token, etc.)
