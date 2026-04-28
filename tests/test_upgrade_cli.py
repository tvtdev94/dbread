"""Tests for ``dbread upgrade`` CLI subcommand."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from io import BytesIO

from dbread import upgrade_cli
from dbread.extras.manager import ExtrasState


def _state(extras: list[str], via: str = "uv-tool") -> ExtrasState:
    return ExtrasState(
        extras=extras, installed_via=via, updated_at=datetime.now(UTC).isoformat(),
    )


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------


def test_help_prints_usage(capsys) -> None:
    code = upgrade_cli.main(["--help"])
    out = capsys.readouterr().out
    assert code == 0
    assert "Usage: dbread upgrade" in out


def test_unknown_flag_returns_two(capsys) -> None:
    code = upgrade_cli.main(["--bogus"])
    err = capsys.readouterr().err
    assert code == 2
    assert "unknown flag" in err


# ---------------------------------------------------------------------------
# --check (dry-run)
# ---------------------------------------------------------------------------


def test_check_prints_current_and_latest(capsys, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_cli, "_current_version", lambda: "0.7.9")
    monkeypatch.setattr(upgrade_cli, "_pypi_latest_version", lambda: "0.8.0")
    code = upgrade_cli.main(["--check"])
    out = capsys.readouterr().out
    assert code == 0
    assert "current: 0.7.9" in out
    assert "latest:  0.8.0" in out
    assert "upgrade available" in out


def test_check_says_up_to_date_when_versions_match(capsys, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_cli, "_current_version", lambda: "0.8.0")
    monkeypatch.setattr(upgrade_cli, "_pypi_latest_version", lambda: "0.8.0")
    code = upgrade_cli.main(["--check"])
    out = capsys.readouterr().out
    assert code == 0
    assert "up-to-date" in out


def test_check_handles_pypi_unreachable(capsys, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_cli, "_current_version", lambda: "0.7.9")
    monkeypatch.setattr(upgrade_cli, "_pypi_latest_version", lambda: None)
    code = upgrade_cli.main(["--check"])
    out = capsys.readouterr().out
    assert code == 0
    assert "could not reach pypi.org" in out


def test_pypi_latest_version_parses_response(monkeypatch) -> None:
    payload = json.dumps({"info": {"version": "1.2.3"}}).encode()

    class _FakeResp(BytesIO):
        def __enter__(self): return self  # noqa: ANN201
        def __exit__(self, *a): pass  # noqa: ANN001, ANN201

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **kw: _FakeResp(payload),
    )
    assert upgrade_cli._pypi_latest_version() == "1.2.3"


def test_pypi_latest_version_handles_network_error(monkeypatch) -> None:
    def _raise(*_a, **_kw):
        import urllib.error
        raise urllib.error.URLError("dns down")

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    assert upgrade_cli._pypi_latest_version() is None


# ---------------------------------------------------------------------------
# Windows pre-check
# ---------------------------------------------------------------------------


def test_windows_check_skipped_on_non_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert upgrade_cli._windows_dbread_running() is False


def test_windows_check_returns_false_when_only_caller_is_running(monkeypatch) -> None:
    # The calling `dbread.exe` always shows up — must NOT abort on just one row.
    monkeypatch.setattr(sys, "platform", "win32")

    class _FakeRun:
        stdout = "Image Name PID\n=== ====\ndbread.exe 1234 Console\n"

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeRun())
    assert upgrade_cli._windows_dbread_running() is False


def test_windows_check_returns_true_when_other_dbread_running(monkeypatch) -> None:
    # Caller + an MCP-server instance = 2 rows → genuine conflict.
    monkeypatch.setattr(sys, "platform", "win32")

    class _FakeRun:
        stdout = (
            "Image Name PID\n=== ====\n"
            "dbread.exe 1234 Console\n"
            "dbread.exe 5678 Services\n"
        )

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeRun())
    assert upgrade_cli._windows_dbread_running() is True


def test_windows_check_returns_false_when_not_running(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")

    class _FakeRun:
        stdout = "INFO: No tasks are running which match the specified criteria.\n"

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeRun())
    assert upgrade_cli._windows_dbread_running() is False


def test_upgrade_aborts_when_windows_dbread_running(capsys, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_cli, "_windows_dbread_running", lambda: True)
    code = upgrade_cli.main([])
    err = capsys.readouterr().err
    assert code == 1
    assert "dbread.exe is running" in err
    assert "--force-windows" in err


def test_force_windows_bypasses_check(capsys, monkeypatch, tmp_path) -> None:
    # Even if Windows-running detector returns True, --force-windows skips it.
    monkeypatch.setattr(upgrade_cli, "_windows_dbread_running", lambda: True)
    # Steer state-load to return uv-tool with extras
    monkeypatch.setattr(upgrade_cli, "load_state", lambda: _state(["postgres"]))
    monkeypatch.setattr(upgrade_cli, "_verify_version_via_subprocess", lambda: "0.8.0")

    captured: dict = {}

    class _FakeRun:
        returncode = 0

    def _fake_subproc(args, **kwargs):
        captured["args"] = args
        return _FakeRun()

    import subprocess
    monkeypatch.setattr(subprocess, "run", _fake_subproc)

    code = upgrade_cli.main(["--force-windows"])
    assert code == 0
    assert captured["args"] == [
        "uv", "tool", "install", "--reinstall", "dbread[postgres]",
    ]


# ---------------------------------------------------------------------------
# Main upgrade flow
# ---------------------------------------------------------------------------


def test_upgrade_uv_tool_path_runs_reinstall(capsys, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_cli, "_windows_dbread_running", lambda: False)
    monkeypatch.setattr(upgrade_cli, "load_state",
                        lambda: _state(["postgres", "mongo"]))
    monkeypatch.setattr(upgrade_cli, "_verify_version_via_subprocess", lambda: "0.8.0")

    captured: dict = {}

    class _FakeRun:
        returncode = 0

    import subprocess
    def _fake(args, **kw):
        captured["args"] = args
        return _FakeRun()

    monkeypatch.setattr(subprocess, "run", _fake)

    code = upgrade_cli.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert captured["args"] == [
        "uv", "tool", "install", "--reinstall", "dbread[mongo,postgres]",
    ]
    assert "Upgraded to dbread 0.8.0" in out
    assert "mongo, postgres" in out


def test_upgrade_subprocess_failure_prints_and_clamps(capsys, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_cli, "_windows_dbread_running", lambda: False)
    monkeypatch.setattr(upgrade_cli, "load_state", lambda: _state(["mongo"]))

    class _FakeRun:
        returncode = 7

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeRun())
    code = upgrade_cli.main([])
    err = capsys.readouterr().err
    # Real uv code surfaced in message, but exit code clamped to 4
    assert code == 4
    assert "upgrade failed (exit 7)" in err


def test_pip_install_method_falls_back_to_manual(capsys, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_cli, "_windows_dbread_running", lambda: False)
    monkeypatch.setattr(upgrade_cli, "load_state", lambda: _state(["mongo"], via="pip"))

    # subprocess.run MUST NOT be called for pip path
    import subprocess
    sentinel = {"called": False}

    def _no_call(*_a, **_kw):
        sentinel["called"] = True
        raise AssertionError("subprocess.run must not be invoked on pip path")

    monkeypatch.setattr(subprocess, "run", _no_call)

    code = upgrade_cli.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert sentinel["called"] is False
    assert "Auto-upgrade only supports uv-tool" in out
    # M4: pip-method now includes the tracked extras in the manual command
    assert 'pip install --upgrade "dbread[mongo]"' in out


def test_state_missing_bootstraps_with_warning(capsys, monkeypatch) -> None:
    # When load_state returns None we bootstrap AND warn the user about the
    # silent driver-shrink risk.
    monkeypatch.setattr(upgrade_cli, "_windows_dbread_running", lambda: False)
    monkeypatch.setattr(upgrade_cli, "load_state", lambda: None)
    monkeypatch.setattr(upgrade_cli, "bootstrap_state",
                        lambda: _state([], via="uv-tool"))
    monkeypatch.setattr(upgrade_cli, "_verify_version_via_subprocess", lambda: "0.8.0")

    captured: dict = {}

    class _FakeRun:
        returncode = 0

    import subprocess
    def _fake(args, **kw):
        captured["args"] = args
        return _FakeRun()
    monkeypatch.setattr(subprocess, "run", _fake)

    code = upgrade_cli.main([])
    err = capsys.readouterr().err
    assert code == 0
    # Bare `dbread` specifier (no brackets) when extras is empty
    assert captured["args"] == ["uv", "tool", "install", "--reinstall", "dbread"]
    # Warning surfaced about bootstrap being used
    assert "no extras state file" in err
    assert "bootstrapping" in err


def test_uv_subprocess_failure_is_clamped_to_exit_four(capsys, monkeypatch) -> None:
    # uv exit code 2 must be clamped to dbread's "external error" code 4 so
    # callers don't mistake it for our own guard-reject (2).
    monkeypatch.setattr(upgrade_cli, "_windows_dbread_running", lambda: False)
    monkeypatch.setattr(upgrade_cli, "load_state", lambda: _state(["mongo"]))

    class _FakeRun:
        returncode = 2  # uv reports 2

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeRun())
    code = upgrade_cli.main([])
    assert code == 4  # clamped


def test_pipx_install_method_uses_pipx_command(capsys, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_cli, "_windows_dbread_running", lambda: False)
    monkeypatch.setattr(
        upgrade_cli, "load_state",
        lambda: _state(["mongo", "postgres"], via="pipx"),
    )
    code = upgrade_cli.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert "pipx upgrade dbread" in out
    assert "pipx inject dbread" in out
    assert "dbread[mongo,postgres]" in out


def test_unknown_install_method_falls_back_to_uv_command(capsys, monkeypatch) -> None:
    monkeypatch.setattr(upgrade_cli, "_windows_dbread_running", lambda: False)
    monkeypatch.setattr(
        upgrade_cli, "load_state",
        lambda: _state(["duckdb"], via="unknown"),
    )
    code = upgrade_cli.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert 'uv tool install --reinstall "dbread[duckdb]"' in out


# ---------------------------------------------------------------------------
# build_reinstall_args (installer.py addition)
# ---------------------------------------------------------------------------


def test_build_reinstall_args_sorts_and_dedupes() -> None:
    from dbread.extras.installer import build_reinstall_args
    assert build_reinstall_args(["mongo", "postgres", "mongo"]) == [
        "uv", "tool", "install", "--reinstall", "dbread[mongo,postgres]",
    ]


def test_build_reinstall_args_empty_extras() -> None:
    from dbread.extras.installer import build_reinstall_args
    assert build_reinstall_args([]) == [
        "uv", "tool", "install", "--reinstall", "dbread",
    ]


# ---------------------------------------------------------------------------
# Path: verify version via subprocess
# ---------------------------------------------------------------------------


def test_verify_version_via_subprocess_returns_stdout(monkeypatch) -> None:
    class _FakeRun:
        returncode = 0
        stdout = "0.8.0\n"

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _FakeRun())
    assert upgrade_cli._verify_version_via_subprocess() == "0.8.0"


def test_verify_version_via_subprocess_handles_failure(monkeypatch) -> None:
    import subprocess

    def _raise(*_a, **_kw):
        raise OSError("python missing")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert upgrade_cli._verify_version_via_subprocess() is None
