# Dev-install dbread as a `uv tool` from the current repo, picking up any
# source changes even when the version number hasn't bumped.
#
# Why: `uv tool install --force <path>` does NOT rebuild the wheel when the
# package version is unchanged — it only refreshes the exe shim. Use
# `--reinstall-package dbread` so uv actually rebuilds + re-copies source.
#
# Usage:  .\scripts\dev-install.ps1
#         .\scripts\dev-install.ps1 -Extras "postgres,mysql,mongo"

param(
    [string]$Extras = "mongo"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Spec = "$RepoRoot[$Extras]"

# Kill any running dbread MCP process so the exe shim isn't locked.
Get-Process dbread -ErrorAction SilentlyContinue | Stop-Process -Force

Write-Host "-> installing dbread from $RepoRoot with extras [$Extras]"
uv tool install --reinstall-package dbread $Spec

Write-Host ""
dbread --version
Write-Host ""
Write-Host "Reconnect dbread in Claude Code: type /mcp and pick 'dbread' -> Reconnect."
