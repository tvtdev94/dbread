"""Extras state management: read/write state file, detect install method, scan drivers."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

# Maps dbread optional-dependency name to the Python module that dependency installs.
EXTRA_TO_MODULE: dict[str, str] = {
    "postgres": "psycopg2",
    "mysql": "pymysql",
    "mssql": "pyodbc",
    "oracle": "oracledb",
    "duckdb": "duckdb",
    "clickhouse": "clickhouse_sqlalchemy",
    "mongo": "pymongo",
}

# Maps dbread Dialect string to its optional-dependency name.
# sqlite intentionally absent — it has no extra (ships with Python stdlib).
DIALECT_TO_EXTRA: dict[str, str] = {
    "postgres": "postgres",
    "mysql": "mysql",
    "mssql": "mssql",
    "oracle": "oracle",
    "duckdb": "duckdb",
    "clickhouse": "clickhouse",
    "mongodb": "mongo",
}


class ExtrasState(BaseModel):
    """Persisted record of which dbread driver extras are installed."""

    extras: list[str]
    installed_via: Literal["uv-tool", "pip", "pipx", "unknown"]
    updated_at: str  # ISO8601 UTC timestamp


def state_path() -> Path:
    """Return the path to the extras state file: ~/.dbread/installed_extras.json."""
    return Path.home() / ".dbread" / "installed_extras.json"


def load_state() -> ExtrasState | None:
    """Load persisted ExtrasState from disk.

    Returns None if the file does not exist or contains invalid JSON/schema.
    """
    path = state_path()
    if not path.exists():
        return None
    try:
        return ExtrasState.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — JSON decode or pydantic validation error
        return None


def save_state(state: ExtrasState) -> None:
    """Atomically write ExtrasState to disk via tempfile + os.replace.

    Creates ~/.dbread/ if it does not yet exist.
    The rename is atomic on POSIX; on Windows it is best-effort (os.replace
    replaces the destination even if it exists, which is safe).
    """
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.model_dump_json(indent=2)

    # Write to a sibling temp file so a crash mid-write leaves the original intact.
    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=".extras_state_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up the orphaned temp file, then re-raise.
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def detect_install_method() -> str:
    """Infer how dbread was installed by inspecting sys.executable path.

    Returns one of: "uv-tool", "pip", "pipx", "unknown".
    """
    exe = sys.executable.replace("\\", "/")

    # uv tool install path patterns
    uv_tool_patterns = (
        "/AppData/Local/uv/tools/dbread/",   # Windows
        ".local/share/uv/tools/dbread/",      # Linux/macOS
        "/uv/tools/dbread/",                  # fallback generic uv path
    )
    for pattern in uv_tool_patterns:
        if pattern in exe:
            return "uv-tool"

    # pipx path pattern
    if "pipx/venvs/dbread" in exe:
        return "pipx"

    # Generic pip install (covers editable installs in site-packages)
    if "site-packages" in exe or "site-packages" in str(Path(sys.executable).parent):
        return "pip"

    return "unknown"


def scan_installed_extras() -> list[str]:
    """Return sorted list of extra names whose driver module is importable.

    Uses importlib.util.find_spec for a cheap, import-side-effect-free check.
    """
    found: list[str] = []
    for extra, module in EXTRA_TO_MODULE.items():
        try:
            spec = importlib.util.find_spec(module)
        except (ModuleNotFoundError, ValueError):
            # find_spec raises ModuleNotFoundError for sub-modules whose parent
            # is missing; ValueError for empty string names.
            spec = None
        if spec is not None:
            found.append(extra)
    return sorted(found)


def bootstrap_state() -> ExtrasState:
    """Create an initial ExtrasState by scanning the current environment.

    Called when the state file is missing or unreadable.
    """
    return ExtrasState(
        extras=scan_installed_extras(),
        installed_via=detect_install_method(),  # type: ignore[arg-type]
        updated_at=datetime.now(tz=UTC).isoformat(),
    )


def merge_extras(current: list[str], new: list[str]) -> list[str]:
    """Return sorted, deduplicated union of current and new extra names."""
    return sorted(set(current) | set(new))
