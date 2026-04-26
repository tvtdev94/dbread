"""Interactive 'add connection' wizard.

Public API:
    run_add_wizard(name, *, from_stdin, no_test, dialect_hint, manual) -> int

Exit codes:
    0 — success
    1 — user cancelled / test failed and declined to save
    2 — no input provided
    3 — unsupported format or driver install failure
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from dbread.connstr import UnknownFormat, UnsupportedConnString, detect_and_parse
from dbread.connstr._manual_entry import (
    _manual_url_entry,
    _offer_fallback_menu,
    _UserCancelledError,
)
from dbread.connstr.converter import DEFAULT_PORT, to_sqlalchemy_url
from dbread.connstr.types import ParsedConn
from dbread.connstr.writers import write_config_yaml, write_env

# ---------------------------------------------------------------------------
# Main wizard entry-point
# ---------------------------------------------------------------------------


def run_add_wizard(
    name: str | None = None,
    *,
    from_stdin: bool = False,
    no_test: bool = False,
    dialect_hint: str | None = None,
    manual: bool = False,
) -> int:
    """Interactive flow: paste → detect → preview → extra-check →
    convert → name → test → write .env → write config.yaml → summary.

    When ``manual=True`` detection is skipped entirely; the user is prompted
    directly for a SQLAlchemy URL.

    Returns exit code (0 ok, 1 cancelled, 2 no input, 3 unsupported).
    """
    # Step 1/2: obtain a ParsedConn — either via detection or manual entry
    if manual:
        try:
            parsed = _manual_url_entry(dialect_hint)
        except _UserCancelledError:
            return 1
    else:
        raw = _read_connection_string(from_stdin)
        if not raw:
            print("No connection string provided.", file=sys.stderr)
            return 2

        try:
            parsed = detect_and_parse(raw, dialect_hint=dialect_hint)
        except UnsupportedConnString as exc:
            print(f"Unsupported connection string: {exc}")
            print(f"Hint: {exc.hint}")
            return 3
        except UnknownFormat as exc:
            print(f"Could not detect format: {exc}")
            result = _offer_fallback_menu(raw, dialect_hint)
            if isinstance(result, int):
                return result
            parsed = result  # ParsedConn from manual entry

    # Step 3: preview
    _print_preview(parsed)

    # Step 4: check driver extra, offer install
    install_result = _check_extra_or_offer_install(parsed.dialect)
    if install_result != 0:
        return install_result

    # Step 5: convert to SQLAlchemy URL
    # For manual format the raw value is already a SQLAlchemy URL — skip converter
    url = parsed.raw if parsed.format == "manual" else to_sqlalchemy_url(parsed)

    # Step 6: prompt for connection name
    default_name = _suggest_name(parsed.database)
    try:
        final_name = _prompt_name(name, default=default_name)
    except _UserCancelledError:
        return 1

    # Check for duplicate in config — offer overwrite. Resolve cfg path lazily
    # so DBREAD_CONFIG env var is honoured at call time.
    from dbread.connstr.writers import _default_cfg_path  # noqa: PLC0415

    if _name_exists_in_config(final_name, _default_cfg_path()) and not _confirm(
        f"Connection {final_name!r} already exists. Overwrite?", default=False
    ):
        return 1

    # Step 7: live connection test (unless --no-test)
    if not no_test:
        ok, err = _test_connection(parsed.dialect, url)
        if not ok:
            print(f"Test failed: {err}")
            result = _handle_test_failure(url, parsed)
            if result is None:
                return 1
            url, parsed = result  # may be updated if user chose "edit"

    # Steps 8-9: write files
    env_path = write_env(final_name, url)
    print(f"Wrote env:    {env_path}")
    cfg_path = write_config_yaml(final_name, parsed)
    print(f"Wrote config: {cfg_path}")

    # Step 10: summary
    _print_summary(final_name, parsed, url)
    return 0


# ---------------------------------------------------------------------------
# Step helpers
# ---------------------------------------------------------------------------


def _read_connection_string(from_stdin: bool) -> str:
    """Read a raw connection string from stdin pipe or interactive prompt."""
    if from_stdin:
        return sys.stdin.read().strip()

    # Interactive: prefer getpass (hides input) but fall back to plain input
    # when stdin is not a TTY (e.g. piped but from_stdin=False).
    import getpass  # noqa: PLC0415

    if sys.stdin.isatty():
        try:
            return getpass.getpass("Paste connection string (hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""
    else:
        print("WARNING: stdin not a TTY, paste will be visible", file=sys.stderr)
        try:
            return input("Paste connection string: ").strip()
        except (EOFError, KeyboardInterrupt):
            return ""


def _print_preview(p: ParsedConn) -> None:
    """Print a table-style summary of the parsed connection (password masked)."""
    port_str = str(p.port) if p.port else str(DEFAULT_PORT.get(p.dialect, "default"))
    host_str = f"{p.host}:{port_str}" if p.host else "-"
    params_str = ", ".join(f"{k}={v}" for k, v in p.params.items()) or "(none)"
    pwd_str = "***" if p.password else "(none)"

    # Human-friendly format label
    fmt_label = "manual (no auto-detection)" if p.format == "manual" else p.format

    print("Detected:")
    print(f"  Format:   {fmt_label}")
    print(f"  Dialect:  {p.dialect}")
    print(f"  Host:     {host_str}")
    print(f"  User:     {p.user or '-'}")
    print(f"  Password: {pwd_str}")
    print(f"  Database: {p.database or '-'}")
    print(f"  Params:   {params_str}")


def _check_extra_or_offer_install(dialect: str) -> int:
    """Check whether the required driver extra is installed; offer to install.

    Returns 0 to continue, 3 to abort.
    """
    from dbread.extras.installer import install_or_print  # noqa: PLC0415
    from dbread.extras.manager import (  # noqa: PLC0415
        DIALECT_TO_EXTRA,
        bootstrap_state,
        detect_install_method,
        load_state,
        merge_extras,
        scan_installed_extras,
    )

    extra = DIALECT_TO_EXTRA.get(dialect)
    if extra is None:
        # sqlite and others with no extra — nothing to check
        return 0

    installed = scan_installed_extras()
    if extra in installed:
        return 0

    print(f"Driver `{extra}` not installed.")
    if not _confirm("Install now?", default=True):
        print(
            f"WARNING: Skipping install. Connection test for dialect "
            f"{dialect!r} will fail with ModuleNotFoundError. "
            f"Run `dbread add-extra {extra}` later to install.",
            file=sys.stderr,
        )
        return 0  # continue; user explicitly declined

    # Bootstrap state if needed
    state = load_state()
    if state is None:
        state = bootstrap_state()

    new_extras = merge_extras(state.extras, [extra])
    method = detect_install_method()
    success = install_or_print(new_extras, method)
    if not success:
        return 3

    # Persist updated state
    from datetime import UTC, datetime  # noqa: PLC0415

    from dbread.extras.manager import ExtrasState, save_state  # noqa: PLC0415

    updated = ExtrasState(
        extras=new_extras,
        installed_via=state.installed_via,
        updated_at=datetime.now(tz=UTC).isoformat(),
    )
    save_state(updated)
    return 0


def _suggest_name(database: str | None) -> str:
    """Slugify the database name to produce a safe connection identifier."""
    if not database:
        return "conn"
    slug = database.lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    slug = slug.strip("_")
    return slug or "conn"


_NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_name(name: str) -> str:
    """Ensure `name` is a valid identifier suitable for an env var key.

    Allowed: alphanumerics + underscore, must start with letter or underscore.
    Rejected: spaces, hyphens, leading digits, special chars.
    """
    if not _NAME_PATTERN.match(name):
        raise ValueError(
            f"Invalid connection name {name!r}. "
            f"Use only letters, digits, and underscores (must start with letter/underscore)."
        )
    return name


def _prompt_name(provided: str | None, default: str) -> str:
    """Ask the user for a connection name; use `provided` if given. Validates."""
    if provided:
        return _validate_name(provided)
    while True:
        try:
            answer = input(f"Connection name [{default}]: ").strip()
        except (EOFError, KeyboardInterrupt) as exc:
            raise _UserCancelledError from exc
        candidate = answer or default
        try:
            return _validate_name(candidate)
        except ValueError as exc:
            print(str(exc))
            # Re-prompt


def _name_exists_in_config(name: str, cfg_path: Path) -> bool:
    """Return True if `name` is already a key under connections: in the config."""
    if not cfg_path.exists():
        return False
    import yaml  # noqa: PLC0415

    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return False
    conns = data.get("connections") or {}
    return name in conns


def _test_connection(dialect: str, url: str) -> tuple[bool, str]:
    """Attempt a live connection. Returns (ok, error_message).

    Thin wrapper around dbread.connstr.health.test_connection — kept for
    backward compatibility with wizard tests that mock this symbol.
    """
    from dbread.connstr.health import test_connection  # noqa: PLC0415
    ok, err, _ms = test_connection(dialect, url, timeout_s=5)
    return ok, err


def _confirm(msg: str, default: bool = False) -> bool:
    """Prompt user for yes/no. Empty input returns `default`."""
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{msg} {hint}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    return answer in {"y", "yes"}


def _handle_test_failure(
    url: str,
    parsed: ParsedConn,
    *,
    _attempt: int = 1,
) -> tuple[str, ParsedConn] | None:
    """Offer recovery options after a failed connection test.

    Returns (new_url, new_parsed) to proceed with save, or None to cancel.
    Caps edit-and-retry at 3 total attempts (i.e. 2 edit retries) to prevent
    infinite loops.  When the cap is reached the function returns None without
    prompting again.
    """
    # Cap reached *before* showing the menu so the caller gets a clean None
    # without needing to consume an extra input token.
    # Limit: 2 edit retries max (attempts 1→2→3 cap).
    if _attempt >= 3:
        print("Max retries reached. Cancelling.")
        return None

    print()
    print("What now?")
    print("  1) Save anyway")
    print("  2) Edit URL and re-test")
    print("  3) Cancel")

    try:
        choice = input("Choice [1/2/3]: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if choice == "1":
        return url, parsed

    if choice == "2":
        try:
            new_parsed = _manual_url_entry(parsed.dialect)
        except _UserCancelledError:
            return None
        new_url = new_parsed.raw
        ok, err = _test_connection(new_parsed.dialect, new_url)
        if ok:
            print("OK  test passed.")
            return new_url, new_parsed
        print(f"Test failed again: {err}")
        return _handle_test_failure(new_url, new_parsed, _attempt=_attempt + 1)

    # option 3 or anything else → cancel
    return None


def _print_summary(name: str, p: ParsedConn, url: str) -> None:
    """Print a short 'what was written' summary after successful save."""
    key = f"{name.upper()}_URL"
    home = Path.home()

    def _short(path: Path) -> str:
        try:
            return "~/" + str(path.relative_to(home)).replace("\\", "/")
        except ValueError:
            return str(path)

    env_path = Path.home() / ".dbread" / ".env"
    cfg_path = Path.home() / ".dbread" / "config.yaml"

    masked_url = _mask_url_password(url)

    print("\nWrote:")
    print(f"  {_short(env_path):<30} {key}={masked_url}")
    print(f"  {_short(cfg_path):<30} {name}: {{ dialect: {p.dialect}, ... }}")
    print("\nTest with: dbread doctor")


_SECRET_QUERY_KEYS = {
    "motherduck_token",
    "token",
    "password",
    "auth_token",
    "api_key",
    "apikey",
    "secret",
}


def _mask_url_password(url: str) -> str:
    """Replace password (userinfo) and known secret query params with '***'."""
    masked = re.sub(r"(://[^:@/]+:)[^@]+(@)", r"\1***\2", url)
    # Mask query-string secrets (case-insensitive key match)
    pattern = re.compile(
        r"([?&](?:" + "|".join(_SECRET_QUERY_KEYS) + r")=)[^&]+",
        flags=re.IGNORECASE,
    )
    return pattern.sub(r"\1***", masked)
