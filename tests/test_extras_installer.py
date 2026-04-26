"""Unit tests for dbread.extras.installer."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dbread.extras.installer import build_install_args, install_or_print, run_install

# ---------------------------------------------------------------------------
# build_install_args
# ---------------------------------------------------------------------------

def test_build_install_args_single_extra():
    args = build_install_args(["postgres"])
    assert args == ["uv", "tool", "install", "--force", "dbread[postgres]"]


def test_build_install_args_multiple_extras_sorted():
    args = build_install_args(["mongo", "postgres"])
    assert args == ["uv", "tool", "install", "--force", "dbread[mongo,postgres]"]


def test_build_install_args_deduplicates():
    args = build_install_args(["postgres", "postgres", "mongo"])
    assert args == ["uv", "tool", "install", "--force", "dbread[mongo,postgres]"]


def test_build_install_args_empty_list():
    args = build_install_args([])
    assert args == ["uv", "tool", "install", "--force", "dbread"]


def test_build_install_args_unicode_extra():
    # Exotic extra name should pass through unchanged
    args = build_install_args(["my-extra-é"])
    assert args[-1] == "dbread[my-extra-é]"


def test_build_install_args_order_is_deterministic():
    # Different input order must produce same output
    a = build_install_args(["duckdb", "mongo", "postgres"])
    b = build_install_args(["postgres", "duckdb", "mongo"])
    assert a == b


# ---------------------------------------------------------------------------
# run_install
# ---------------------------------------------------------------------------

def test_run_install_dry_run_returns_zero(capsys):
    rc, stdout, stderr = run_install(["postgres"], dry_run=True)
    assert rc == 0
    assert stdout == ""
    assert stderr == ""
    captured = capsys.readouterr()
    assert "dry-run" in captured.out
    assert "postgres" in captured.out


def test_run_install_calls_subprocess(monkeypatch: pytest.MonkeyPatch):
    fake_result = MagicMock()
    fake_result.returncode = 0
    fake_result.stdout = "installed"
    fake_result.stderr = ""

    with patch("dbread.extras.installer.subprocess.run", return_value=fake_result) as mock_run:
        rc, out, err = run_install(["duckdb"])

    assert rc == 0
    assert out == "installed"
    assert err == ""
    call_args = mock_run.call_args
    assert call_args.kwargs["shell"] is False
    assert call_args.kwargs["text"] is True
    assert call_args.kwargs["capture_output"] is True


def test_run_install_returns_nonzero_on_failure():
    fake_result = MagicMock()
    fake_result.returncode = 1
    fake_result.stdout = ""
    fake_result.stderr = "error: package not found"

    with patch("dbread.extras.installer.subprocess.run", return_value=fake_result):
        rc, _out, err = run_install(["nonexistent"])

    assert rc == 1
    assert "error" in err


# ---------------------------------------------------------------------------
# install_or_print
# ---------------------------------------------------------------------------

def test_install_or_print_uv_tool_success(capsys):
    fake_result = MagicMock()
    fake_result.returncode = 0

    with patch("dbread.extras.installer.subprocess.run", return_value=fake_result) as mock_run:
        result = install_or_print(["postgres"], "uv-tool")

    assert result is True
    call_args = mock_run.call_args
    assert call_args.kwargs["shell"] is False
    assert call_args.kwargs["capture_output"] is False  # live streaming
    captured = capsys.readouterr()
    assert "running:" in captured.out


def test_install_or_print_uv_tool_failure(capsys):
    fake_result = MagicMock()
    fake_result.returncode = 2

    with patch("dbread.extras.installer.subprocess.run", return_value=fake_result):
        result = install_or_print(["postgres"], "uv-tool")

    assert result is False
    captured = capsys.readouterr()
    assert "failed" in captured.err


def test_install_or_print_pip_prints_manual_command(capsys):
    result = install_or_print(["mysql"], "pip")
    assert result is False
    captured = capsys.readouterr()
    assert "pip install" in captured.out
    assert "mysql" in captured.out


def test_install_or_print_pipx_prints_uv_command(capsys):
    result = install_or_print(["mongo"], "pipx")
    assert result is False
    captured = capsys.readouterr()
    assert "uv tool install" in captured.out
    assert "mongo" in captured.out


def test_install_or_print_unknown_prints_uv_command(capsys):
    result = install_or_print(["duckdb"], "unknown")
    assert result is False
    captured = capsys.readouterr()
    assert "uv tool install" in captured.out
    assert "duckdb" in captured.out


def test_install_or_print_pip_does_not_call_subprocess():
    with patch("dbread.extras.installer.subprocess.run") as mock_run:
        install_or_print(["postgres"], "pip")
    mock_run.assert_not_called()


def test_install_or_print_unknown_does_not_call_subprocess():
    with patch("dbread.extras.installer.subprocess.run") as mock_run:
        install_or_print(["postgres"], "unknown")
    mock_run.assert_not_called()
