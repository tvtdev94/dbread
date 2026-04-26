"""File writers for .env and config.yaml — comment-preserving, backup-safe.

Public API:
    write_env(name, url, *, env_path=None) -> Path
    write_config_yaml(name, p, *, cfg_path=None) -> Path
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from pathlib import Path

import yaml

from dbread.connstr.types import ParsedConn

# ---------------------------------------------------------------------------
# Default paths (DBREAD_CONFIG-aware)
# ---------------------------------------------------------------------------


def _default_cfg_path() -> Path:
    """Resolve config.yaml path. Honours DBREAD_CONFIG env var."""
    override = os.environ.get("DBREAD_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".dbread" / "config.yaml"


def _default_env_path() -> Path:
    """Resolve .env path. Lives next to the resolved config.yaml."""
    return _default_cfg_path().parent / ".env"


# Backwards-compat module-level aliases (callers may import these names).
# Resolved at import time but tests usually pass explicit paths.
_DEFAULT_CFG = _default_cfg_path()
_DEFAULT_ENV = _default_env_path()


# ---------------------------------------------------------------------------
# Public writers
# ---------------------------------------------------------------------------


def write_env(name: str, url: str, *, env_path: Path | None = None) -> Path:
    """Write NAME_URL=<url> into the .env file.

    Replace existing KEY=... in-place if it already exists; otherwise append.
    Creates the file (and parent dir) if missing. Makes a .bak on first edit.
    Returns the path written.
    """
    path = env_path or _default_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    key = f"{name.upper()}_URL"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    _make_backup(path)

    new_text = _replace_or_append_env_line(existing, key, url)
    path.write_text(new_text, encoding="utf-8")
    # Tighten perms — .env contains secrets. POSIX only; Windows ignores chmod
    # in any meaningful way (use ACLs there).
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)  # best-effort; .env contains secrets
    return path


def write_config_yaml(name: str, p: ParsedConn, *, cfg_path: Path | None = None) -> Path:
    """Insert a YAML connection block under the 'connections:' key.

    Preserves comments. Validates the result via yaml.safe_load and rolls back
    to the .bak (or original captured text) on parse failure.
    Raises ValueError if the connection name already exists.
    Returns the path written.
    """
    path = cfg_path or _default_cfg_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    original = path.read_text(encoding="utf-8") if path.exists() else "connections:\n"

    # Idempotency guard — check if name already in use
    try:
        parsed_yaml = yaml.safe_load(original) or {}
    except yaml.YAMLError:
        parsed_yaml = {}
    conns = parsed_yaml.get("connections") or {}
    if name in conns:
        raise ValueError(f"connection {name!r} already exists")

    _make_backup(path)

    block = _render_connection_block(name, p)
    try:
        new_text = _insert_yaml_connection(original, name, block)
    except ValueError:
        raise  # propagate "connections: missing" clearly

    # Validate the result parses cleanly
    try:
        yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        # Restore from backup if available, else write original back
        bak = Path(str(path) + ".bak")
        restore_text = bak.read_text(encoding="utf-8") if bak.exists() else original
        path.write_text(restore_text, encoding="utf-8")
        raise ValueError(f"YAML validation failed after insert: {exc}") from exc

    path.write_text(new_text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Backup helper
# ---------------------------------------------------------------------------


def _make_backup(path: Path) -> None:
    """Write {path}.bak only if .bak does not already exist.

    This preserves the original-first-version semantics: subsequent writes
    do not overwrite the very first backup.
    """
    bak = Path(str(path) + ".bak")
    if bak.exists():
        return
    if path.exists():
        bak.write_bytes(path.read_bytes())


# ---------------------------------------------------------------------------
# .env text manipulation
# ---------------------------------------------------------------------------


def _replace_or_append_env_line(text: str, key: str, value: str) -> str:
    """Replace `^KEY=.*$` (multiline) or append `KEY=value` if not found.

    Always ensures the result ends with a newline.
    """
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    replacement = f"{key}={value}"

    if pattern.search(text):
        new_text = pattern.sub(replacement, text)
    else:
        # Ensure a trailing newline before appending
        if text and not text.endswith("\n"):
            text += "\n"
        new_text = text + replacement + "\n"

    # Guarantee trailing newline
    if not new_text.endswith("\n"):
        new_text += "\n"
    return new_text


# ---------------------------------------------------------------------------
# YAML text manipulation
# ---------------------------------------------------------------------------


def _render_connection_block(name: str, p: ParsedConn) -> str:
    """Generate the indented YAML snippet for a single connection.

    Uses 2-space indent (standard for the generated config template).
    """
    name_upper = name.upper()
    lines = [
        f"  {name}:",
        f"    url_env: {name_upper}_URL",
        f"    dialect: {p.dialect}",
        "    rate_limit_per_min: 60",
        "    statement_timeout_s: 30",
        "    max_rows: 1000",
    ]
    if p.dialect == "mongodb":
        lines += [
            "    mongo:",
            "      sample_size: 100",
        ]
    return "\n".join(lines) + "\n"


def _insert_yaml_connection(text: str, name: str, block: str) -> str:  # noqa: ARG001
    """Insert block under 'connections:' before the next top-level key or EOF.

    Strategy:
    1. Find the 'connections:' line index.
    2. Find the next top-level key line (zero-indent, non-blank, non-comment)
       after the connections block, or use EOF.
    3. Insert the block immediately before that line.

    Raises ValueError if 'connections:' is missing entirely.
    """
    lines = text.splitlines(keepends=True)

    # --- Find the 'connections:' line ---
    conn_idx: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("connections:"):
            conn_idx = i
            break

    if conn_idx is None:
        raise ValueError("'connections:' key not found in config YAML")

    # --- Find next top-level key after connections block ---
    # A top-level key starts at column 0, is not blank and not a comment.
    insert_before: int = len(lines)  # default: EOF
    for i in range(conn_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue  # blank line — may still be inside connections
        if line[0] not in (" ", "\t", "#", "\n", "\r"):
            # Non-indented, non-comment, non-blank → top-level key
            insert_before = i
            break

    # Insert the block lines before the found position
    block_lines = block.splitlines(keepends=True)
    # Ensure blank separator before top-level key if inserting mid-file
    result_lines = lines[:insert_before] + block_lines + lines[insert_before:]
    return "".join(result_lines)
