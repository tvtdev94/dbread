"""Subprocess wrapper for `uv tool install --force` and manual-install guidance."""

from __future__ import annotations

import subprocess
import sys


def build_install_args(extras: list[str]) -> list[str]:
    """Return the argv list for a forced uv-tool reinstall with the given extras.

    The extras are sorted and deduplicated so the command is deterministic.
    An empty extras list produces a bare 'dbread' specifier (no brackets).

    Example:
        build_install_args(["mongo", "postgres"])
        -> ["uv", "tool", "install", "--force", "dbread[mongo,postgres]"]
    """
    unique_sorted = sorted(set(extras))
    specifier = f"dbread[{','.join(unique_sorted)}]" if unique_sorted else "dbread"
    return ["uv", "tool", "install", "--force", specifier]


def run_install(
    extras: list[str],
    *,
    dry_run: bool = False,
) -> tuple[int, str, str]:
    """Run `uv tool install --force dbread[...]` as a subprocess.

    Args:
        extras:   List of extra names to include in the install specifier.
        dry_run:  If True, print the command instead of executing it.

    Returns:
        (returncode, stdout, stderr) — all strings.
    """
    args = build_install_args(extras)
    if dry_run:
        print("dry-run:", " ".join(args))
        return (0, "", "")

    result = subprocess.run(  # noqa: S603 — shell=False, args validated above
        args,
        shell=False,
        text=True,
        capture_output=True,
    )
    return (result.returncode, result.stdout, result.stderr)


def install_or_print(extras: list[str], install_method: str) -> bool:
    """Conditionally run install or print the manual command.

    For 'uv-tool' installs: executes the install, streams stdout/stderr to the
    terminal, and returns True on success.

    For all other install methods ('pip', 'pipx', 'unknown'): prints the
    appropriate manual command and returns False — never auto-executes because
    re-installing into pip/pipx envs may require privileged or user-specific steps.

    Args:
        extras:          Extra names to install.
        install_method:  One of "uv-tool", "pip", "pipx", "unknown".

    Returns:
        True if the installation was attempted and succeeded, False otherwise.
    """
    if install_method == "uv-tool":
        args = build_install_args(extras)
        print("running:", " ".join(args))
        result = subprocess.run(  # noqa: S603 — shell=False, args built internally
            args,
            shell=False,
            text=True,
            capture_output=False,  # stream live to terminal
        )
        if result.returncode != 0:
            print(
                f"install failed (exit {result.returncode})",
                file=sys.stderr,
            )
        return result.returncode == 0

    # For non-uv-tool installs, surface the right manual command.
    unique_sorted = sorted(set(extras))
    specifier = f"dbread[{','.join(unique_sorted)}]" if unique_sorted else "dbread"

    if install_method == "pip":
        cmd = f'pip install --upgrade "{specifier}"'
    else:
        # pipx and unknown both use uv tool install (safest universal advice)
        cmd = f'uv tool install --force "{specifier}"'

    print(
        f"Cannot auto-install (detected install method: {install_method!r}).\n"
        f"Run manually:\n\n  {cmd}\n"
    )
    return False
