# v0.7.0: Smart Extras Tracking + Interactive Connection Wizard

**Date:** 2026-04-26 09:00–17:30  
**Severity:** Feature Release  
**Component:** CLI, extras management, connection string handling  
**Status:** Shipped

## What Shipped

1. **Smart Extras Tracking**  
   State file `~/.dbread/installed_extras.json` tracks which optional deps are installed. New CLI commands: `dbread add-extra` (bootstraps missing extras), `dbread list-extras` (shows current state), `dbread doctor` (diagnoses install problems). Detection via importlib.util.find_spec — no runtime overhead.

2. **Interactive Add Wizard**  
   `dbread add` now prompts for connection string (hidden input via getpass) → auto-detects 6 format families (URI, JDBC, ADO.NET, ODBC, cloud endpoints, file paths) across all 8 dialects → URL-escapes password → live test (SELECT 1 / Mongo ping) → writes .env + config.yaml preserving user comments. Blocks unsupported patterns (Trusted_Connection, TNS aliases, MSSQL named instances) with helpful hints.

## Why This Matters

User pain point #1: `uv tool install dbread[mongo]` after `dbread[postgres]` wipes psycopg2 because uv-tool doesn't isolate extras. Smart tracking lets users reinstall cleanly without guessing which extras to add.

User pain point #2: Adding new connections required manual YAML/env splitting + password format conversion. Interactive wizard + auto-detection removes friction, catches format errors early via test connection.

## Architecture Decisions

**Zero new runtime deps.** No ruamel.yaml (used raw-text YAML insertion preserving comments). No pyperclip (getpass + --from-stdin only). Keeps startup cheap, avoids native dependency headaches.

**Modularization by concern:** `src/dbread/extras/` (manager, installer), `src/dbread/connstr/` (types, detector, 6 parsers, converter, wizard, writers). Each file ≤200 LOC. Snake_case throughout (matches project convention).

**Lazy imports in wizard.** SQLAlchemy, pymongo only imported inside test_connection() — avoids startup penalty if user just wants `dbread list-extras`.

**Default test connection.** `SELECT 1` / Mongo ping runs by default; --no-test opt-out. Fail-fast catches format/auth errors immediately instead of silently dropping bad configs.

## Code Review Caught

- SERVER_VERSION mismatch (0.6.0 in server.py vs 0.7.0 in pyproject.toml) — fixed
- .env not chmod 0o600 on POSIX — passwords world-readable by accident — fixed
- Wizard ignored DBREAD_CONFIG env var (diverged from doctor behavior) — fixed
- Connection name not validated → spaces broke env key format — now rejected with message
- MotherDuck tokens + query secrets printed in clear in summary — now redacted
- Duplicate query keys silently dropped in URI parser — now warns
- User declining extra install gave cryptic failure later — now explicit error upfront

## Test Results

526 passed (+252 new tests from v0.6's 274) | ruff clean | 0 regressions.

4 fail + 10 errors pre-existing (pymongo not in dev env, no Docker). Didn't block ship.

## Next Dev Session

1. Consider caching extras detection result (currently re-scans on every command) — measure before optimizing
2. Expand wizard to support editing existing connections (currently add-only)
3. Monitor user feedback on format detection accuracy (6 parsers are heuristic-based)
4. Migrate to ruamel.yaml if we need programmatic YAML editing beyond insertion

## Files Changed

See git log `ca1e479..HEAD` and code-review report in plans/ for detailed changes.

---

**Status:** DONE  
**Summary:** v0.7.0 ships smart extras tracking + interactive wizard, eliminating install state confusion and connection string friction — tested (526 pass), reviewed (7 findings fixed), ready for release.
