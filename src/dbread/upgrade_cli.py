"""``dbread upgrade`` CLI subcommand — preserve tracked extras across upgrade.

Wraps ``uv tool install --reinstall "dbread[<tracked-extras>]"`` so a user
who installed via :func:`dbread add-extra` keeps their drivers across
``uv tool upgrade``-style refreshes (which would otherwise drop extras).

On Windows, runs a ``tasklist`` pre-check to detect a running ``dbread.exe``
(file-lock risk during install). Use ``--force-windows`` to bypass.

``--check`` is a dry-run that prints current vs latest PyPI version without
installing anything.

Zero new runtime dependencies — stdlib only (``urllib.request``,
``subprocess``, ``importlib.metadata``).
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from importlib.metadata import PackageNotFoundError, version

from .extras.installer import build_reinstall_args
from .extras.manager import bootstrap_state, load_state

_USAGE = """\
Usage: dbread upgrade [opts]

Options:
  --check              Dry-run: print current vs latest PyPI version
  --force-windows      Skip the Windows tasklist pre-check
  --help, -h           Print this help

Behaviour:
  Re-runs `uv tool install --reinstall dbread[<tracked extras>]` so the
  driver extras you installed via `dbread add-extra` survive the upgrade.

  On Windows, aborts when `dbread.exe` is detected as running (file lock).
  Bypass with --force-windows.

  Falls back to a manual command if `dbread` was installed via pip / pipx
  (state file missing or installed_via != "uv-tool").
"""

_PYPI_TIMEOUT_S = 10
_TASKLIST_TIMEOUT_S = 5


def main(args: list[str]) -> int:
    parsed, code = _parse_args(args)
    if parsed is None:
        return code

    if parsed["check"]:
        return _do_check()

    if not parsed["force_windows"] and _windows_dbread_running():
        print(
            "abort: dbread.exe is running (Windows holds the file open).\n"
            "Close Claude Code (or any other dbread MCP client), then retry.\n"
            "Override with --force-windows if you accept the risk.",
            file=sys.stderr,
        )
        return 1

    state = load_state()
    if state is None:
        state = bootstrap_state()
        print(
            "warning: no extras state file found — bootstrapping from "
            "currently importable drivers. Any previously-installed driver "
            f"that is broken or missing will NOT be reinstalled.\n"
            f"Detected: extras={list(state.extras)} via={state.installed_via!r}\n"
            "If the list looks wrong, re-add via `dbread add-extra <name>` first.\n",
            file=sys.stderr,
        )

    if state.installed_via != "uv-tool":
        manual = _manual_upgrade_command(state.installed_via, state.extras)
        print(
            f"Detected install method: {state.installed_via!r}. "
            "Auto-upgrade only supports uv-tool installs.\n\n"
            f"Run manually:\n  {manual}\n",
        )
        return 0

    args_argv = build_reinstall_args(state.extras)
    print("running:", " ".join(args_argv))
    result = subprocess.run(  # noqa: S603 — argv built internally, no shell
        args_argv, shell=False, text=True, capture_output=False,
    )
    if result.returncode != 0:
        print(f"upgrade failed (exit {result.returncode})", file=sys.stderr)
        # Clamp uv exit code to dbread's "connection/external error" bucket
        # so callers don't confuse it with our own guard / rate-limit codes.
        return 4

    new = _verify_version_via_subprocess()
    extras_part = ", ".join(sorted(state.extras)) if state.extras else "(none)"
    print(
        f"\n[OK] Upgraded to dbread {new or '?'} with extras: {extras_part}\n"
        "Restart Claude Code (or your MCP client) to pick up the new server.",
    )
    return 0


def _manual_upgrade_command(install_method: str, extras: list[str]) -> str:
    """Pick the right manual upgrade command for the detected install method."""
    spec = sorted(set(extras))
    specifier = f"dbread[{','.join(spec)}]" if spec else "dbread"
    if install_method == "pip":
        return f'pip install --upgrade "{specifier}"'
    if install_method == "pipx":
        return f'pipx upgrade dbread  # then: pipx inject dbread "{specifier}"'
    # unknown / anything else
    return f'uv tool install --reinstall "{specifier}"'


def _parse_args(args: list[str]) -> tuple[dict | None, int]:
    check = False
    force_windows = False
    for a in args:
        if a in ("-h", "--help"):
            print(_USAGE)
            return None, 0
        if a == "--check":
            check = True
        elif a == "--force-windows":
            force_windows = True
        else:
            print(f"error: unknown flag: {a}\n\n{_USAGE}", file=sys.stderr)
            return None, 2
    return {"check": check, "force_windows": force_windows}, 0


def _do_check() -> int:
    cur = _current_version() or "?"
    latest = _pypi_latest_version()
    if latest is None:
        print(f"current: {cur}\nlatest:  (could not reach pypi.org)")
        return 0
    label = "up-to-date" if cur == latest else "upgrade available"
    print(f"current: {cur}\nlatest:  {latest}  ({label})")
    return 0


def _current_version() -> str | None:
    try:
        return version("dbread")
    except PackageNotFoundError:
        return None


def _pypi_latest_version() -> str | None:
    try:
        with urllib.request.urlopen(  # noqa: S310 — fixed pypi.org HTTPS URL
            "https://pypi.org/pypi/dbread/json", timeout=_PYPI_TIMEOUT_S,
        ) as r:
            data = json.load(r)
        v = data.get("info", {}).get("version")
        return str(v) if v else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def _windows_dbread_running() -> bool:
    """True when ANOTHER ``dbread.exe`` is in tasklist (excludes ourselves).

    The uv-tool entry point is itself ``dbread.exe`` so the calling process
    always shows up. We count occurrences and abort only when MORE than one
    is present (i.e. an MCP server or other client is also running). Soft-
    fail to False on any tasklist error.
    """
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(  # noqa: S603, S607 — tasklist is built-in Windows
            ["tasklist", "/FI", "IMAGENAME eq dbread.exe"],
            capture_output=True, text=True, timeout=_TASKLIST_TIMEOUT_S,
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return False
    # Each running process produces one line containing "dbread.exe"; the
    # header lines never do. Counting > 1 leaves room for the calling proc.
    occurrences = sum(1 for line in out.splitlines() if "dbread.exe" in line)
    return occurrences > 1


def _verify_version_via_subprocess() -> str | None:
    """Spawn a fresh interpreter to read the post-upgrade version.

    Avoids stale-module bugs from importing in-process after the install
    overwrote our own files.
    """
    snippet = (
        "from importlib.metadata import version; print(version('dbread'))"
    )
    try:
        out = subprocess.run(  # noqa: S603 — fixed argv
            [sys.executable, "-c", snippet],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None
