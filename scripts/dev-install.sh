#!/usr/bin/env bash
# Dev-install dbread as a `uv tool` from the current repo, picking up any
# source changes even when the version number hasn't bumped.
#
# Why this script exists: `uv tool install --force <path>` does NOT rebuild
# the wheel when the package version is unchanged — it only refreshes the
# exe shim. For iterative dev (edit source -> test via MCP) you need
# `--reinstall-package <name>` so uv actually rebuilds and re-copies source.
#
# Usage:  bash scripts/dev-install.sh
#         bash scripts/dev-install.sh postgres,mysql,mongo   # extras
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# On MSYS/Git-Bash (Windows), translate /c/... → C:/... so uv resolves the path.
if command -v cygpath >/dev/null 2>&1; then
  REPO_ROOT="$(cygpath -m "$REPO_ROOT")"
fi
EXTRAS="${1:-mongo}"
SPEC="${REPO_ROOT}[${EXTRAS}]"

# Kill any running dbread MCP process so the exe shim isn't locked on Windows.
if command -v powershell >/dev/null 2>&1; then
  powershell -NoProfile -Command "Get-Process dbread -ErrorAction SilentlyContinue | Stop-Process -Force" || true
elif command -v pkill >/dev/null 2>&1; then
  pkill -f "dbread" 2>/dev/null || true
fi

echo "→ installing dbread from ${REPO_ROOT} with extras [${EXTRAS}]"
uv tool install --reinstall-package dbread "${SPEC}"

echo
dbread --version
echo
echo "Reconnect dbread in Claude Code: type /mcp and pick 'dbread' → Reconnect."
