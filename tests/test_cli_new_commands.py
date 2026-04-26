"""Integration tests for cmd_add, cmd_add_extra, cmd_list_extras, cmd_doctor."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch  # noqa: F401 — MagicMock used in tests

import pytest

from dbread.cli import cmd_add, cmd_add_extra, cmd_doctor, cmd_list_extras
from dbread.extras.manager import ExtrasState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    extras: list[str],
    installed_via: str = "uv-tool",
    updated_at: str = "2026-01-01T00:00:00+00:00",
) -> ExtrasState:
    return ExtrasState(extras=extras, installed_via=installed_via, updated_at=updated_at)


def _minimal_config_yaml(dialect: str = "postgres") -> str:
    """Return a minimal valid config.yaml string for the given dialect."""
    if dialect == "sqlite":
        url_line = "url: sqlite:///file:/tmp/test.db?mode=ro&uri=true"
    else:
        url_line = "url_env: TEST_DB_URL"
    return textwrap.dedent(f"""\
        connections:
          testconn:
            {url_line}
            dialect: {dialect}
        audit:
          path: /tmp/audit.jsonl
    """)


# ---------------------------------------------------------------------------
# cmd_add_extra
# ---------------------------------------------------------------------------


def test_cmd_add_extra_no_args_returns_2(capsys: pytest.CaptureFixture) -> None:
    rc = cmd_add_extra([])
    assert rc == 2
    out = capsys.readouterr().out
    assert "Usage:" in out
    assert "Available extras:" in out


def test_cmd_add_extra_unknown_extra_returns_2(capsys: pytest.CaptureFixture) -> None:
    rc = cmd_add_extra(["bogus"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "Unknown extras" in out
    assert "bogus" in out
    assert "Available:" in out


def test_cmd_add_extra_already_installed_returns_0(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(["postgres"])
    monkeypatch.setattr("dbread.cli.load_state", lambda: state)
    rc = cmd_add_extra(["postgres"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already installed" in out


def test_cmd_add_extra_new_extra_calls_save_state_on_success(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(["postgres"])
    saved: list[ExtrasState] = []

    monkeypatch.setattr("dbread.cli.load_state", lambda: state)
    monkeypatch.setattr("dbread.cli.install_or_print", lambda extras, via: True)
    monkeypatch.setattr("dbread.cli.save_state", lambda s: saved.append(s))

    rc = cmd_add_extra(["mongo"])
    assert rc == 0
    assert len(saved) == 1
    assert "mongo" in saved[0].extras
    assert "postgres" in saved[0].extras
    out = capsys.readouterr().out
    assert "extras now:" in out


def test_cmd_add_extra_install_failure_returns_3_no_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(["postgres"])
    saved: list[ExtrasState] = []

    monkeypatch.setattr("dbread.cli.load_state", lambda: state)
    monkeypatch.setattr("dbread.cli.install_or_print", lambda extras, via: False)
    monkeypatch.setattr("dbread.cli.save_state", lambda s: saved.append(s))

    rc = cmd_add_extra(["mongo"])
    assert rc == 3
    assert len(saved) == 0


def test_cmd_add_extra_bootstraps_when_no_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrapped = _make_state([])
    saved: list[ExtrasState] = []

    monkeypatch.setattr("dbread.cli.load_state", lambda: None)
    monkeypatch.setattr("dbread.cli.bootstrap_state", lambda: bootstrapped)
    monkeypatch.setattr("dbread.cli.install_or_print", lambda extras, via: True)
    monkeypatch.setattr("dbread.cli.save_state", lambda s: saved.append(s))

    rc = cmd_add_extra(["postgres"])
    assert rc == 0
    assert len(saved) == 1
    assert "postgres" in saved[0].extras


# ---------------------------------------------------------------------------
# cmd_list_extras
# ---------------------------------------------------------------------------


def test_cmd_list_extras_returns_0_and_shows_table(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _make_state(["postgres"], installed_via="uv-tool")
    monkeypatch.setattr("dbread.cli.load_state", lambda: state)
    monkeypatch.setattr("dbread.cli.scan_installed_extras", lambda: ["postgres"])

    rc = cmd_list_extras()
    assert rc == 0
    out = capsys.readouterr().out

    # Table header columns present
    assert "EXTRA" in out
    assert "TRACKED" in out
    assert "IMPORTABLE" in out

    # postgres row shows yes/yes
    lines = out.splitlines()
    postgres_line = next((ln for ln in lines if ln.startswith("postgres")), None)
    assert postgres_line is not None
    assert "yes" in postgres_line

    # Install method shown
    assert "uv-tool" in out


def test_cmd_list_extras_no_state_shows_missing_message(
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dbread.cli.load_state", lambda: None)
    monkeypatch.setattr("dbread.cli.scan_installed_extras", lambda: [])
    monkeypatch.setattr("dbread.cli.detect_install_method", lambda: "unknown")

    rc = cmd_list_extras()
    assert rc == 0
    out = capsys.readouterr().out
    assert "missing" in out.lower()


# ---------------------------------------------------------------------------
# cmd_doctor
# ---------------------------------------------------------------------------


def test_cmd_doctor_no_config_returns_3(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Point home to tmp_path — no ~/.dbread/config.yaml there.
    monkeypatch.delenv("DBREAD_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    rc = cmd_doctor()
    assert rc == 3
    out = capsys.readouterr().out
    assert "No config" in out
    assert "dbread init" in out


def test_cmd_doctor_config_with_missing_extra_returns_3(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Write a config with postgres dialect.
    dbread_dir = tmp_path / ".dbread"
    dbread_dir.mkdir()
    cfg_file = dbread_dir / "config.yaml"
    cfg_file.write_text(_minimal_config_yaml("postgres"), encoding="utf-8")

    monkeypatch.delenv("DBREAD_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Simulate postgres driver NOT installed.
    monkeypatch.setattr("dbread.cli.scan_installed_extras", lambda: [])
    # Provide the env var so config can parse url_env reference.
    monkeypatch.setenv("TEST_DB_URL", "postgresql+psycopg2://user:pw@localhost/db")

    rc = cmd_doctor()
    assert rc == 3
    out = capsys.readouterr().out
    assert "MISSING" in out
    assert "postgres" in out
    # Fix command present.
    assert "add-extra" in out


def test_cmd_doctor_all_drivers_present_returns_0(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dbread_dir = tmp_path / ".dbread"
    dbread_dir.mkdir()
    cfg_file = dbread_dir / "config.yaml"
    cfg_file.write_text(_minimal_config_yaml("postgres"), encoding="utf-8")

    monkeypatch.delenv("DBREAD_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # Simulate postgres extra IS installed AND its driver imports successfully.
    monkeypatch.setattr("dbread.cli.scan_installed_extras", lambda: ["postgres"])
    monkeypatch.setattr("importlib.import_module", lambda name: None)  # noqa: ARG005
    monkeypatch.setenv("TEST_DB_URL", "postgresql+psycopg2://user:pw@localhost/db")

    rc = cmd_doctor()
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_cmd_doctor_sqlite_dialect_needs_no_extra(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dbread_dir = tmp_path / ".dbread"
    dbread_dir.mkdir()
    cfg_file = dbread_dir / "config.yaml"
    cfg_file.write_text(_minimal_config_yaml("sqlite"), encoding="utf-8")

    monkeypatch.delenv("DBREAD_CONFIG", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    # No extras installed, but sqlite needs none.
    monkeypatch.setattr("dbread.cli.scan_installed_extras", lambda: [])

    rc = cmd_doctor()
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_cmd_doctor_uses_env_var_config_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_file = tmp_path / "custom_config.yaml"
    cfg_file.write_text(_minimal_config_yaml("sqlite"), encoding="utf-8")

    monkeypatch.setenv("DBREAD_CONFIG", str(cfg_file))
    monkeypatch.setattr("dbread.cli.scan_installed_extras", lambda: [])

    rc = cmd_doctor()
    assert rc == 0


# ---------------------------------------------------------------------------
# cmd_add
# ---------------------------------------------------------------------------


def test_cmd_add_unknown_flag_returns_2(capsys: pytest.CaptureFixture) -> None:
    rc = cmd_add(["--unknown"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "Unknown flag" in out


def test_cmd_add_dialect_hint_missing_value_returns_2(
    capsys: pytest.CaptureFixture,
) -> None:
    rc = cmd_add(["--dialect-hint"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "--dialect-hint requires a value" in out


def test_cmd_add_no_args_delegates_to_wizard() -> None:
    mock_wizard = MagicMock(return_value=0)

    with patch("dbread.connstr.wizard.run_add_wizard", mock_wizard):
        rc = cmd_add([])

    # Wizard was called with correct defaults — manual=False included.
    mock_wizard.assert_called_once_with(
        None, from_stdin=False, no_test=False, dialect_hint=None, manual=False
    )
    assert rc == 0


def test_cmd_add_parses_all_flags_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_kwargs: dict = {}

    def fake_wizard(name, *, from_stdin, no_test, dialect_hint, manual):
        call_kwargs.update(
            name=name,
            from_stdin=from_stdin,
            no_test=no_test,
            dialect_hint=dialect_hint,
            manual=manual,
        )
        return 0

    with patch("dbread.connstr.wizard.run_add_wizard", fake_wizard):
        rc = cmd_add(["myname", "--no-test", "--from-stdin"])

    assert rc == 0
    assert call_kwargs["name"] == "myname"
    assert call_kwargs["from_stdin"] is True
    assert call_kwargs["no_test"] is True
    assert call_kwargs["dialect_hint"] is None
    assert call_kwargs["manual"] is False


def test_cmd_add_dialect_hint_passed_to_wizard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_kwargs: dict = {}

    def fake_wizard(name, *, from_stdin, no_test, dialect_hint, manual):
        call_kwargs.update(
            name=name, from_stdin=from_stdin, no_test=no_test,
            dialect_hint=dialect_hint, manual=manual,
        )
        return 0

    with patch("dbread.connstr.wizard.run_add_wizard", fake_wizard):
        rc = cmd_add(["--dialect-hint", "pg"])

    assert rc == 0
    assert call_kwargs["dialect_hint"] == "pg"
    assert call_kwargs["name"] is None
    assert call_kwargs["manual"] is False


def test_cmd_add_unexpected_positional_returns_2(
    capsys: pytest.CaptureFixture,
) -> None:
    rc = cmd_add(["first", "second"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "Unexpected positional arg" in out


def test_cmd_add_wizard_returning_1_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with patch("dbread.connstr.wizard.run_add_wizard", return_value=1):
        rc = cmd_add([])
    assert rc == 1


# ---------------------------------------------------------------------------
# --manual flag propagation
# ---------------------------------------------------------------------------


def test_cmd_add_manual_flag_propagates_to_wizard() -> None:
    """--manual flag must arrive at run_add_wizard as manual=True."""
    mock_wizard = MagicMock(return_value=0)

    with patch("dbread.connstr.wizard.run_add_wizard", mock_wizard):
        rc = cmd_add(["--manual"])

    mock_wizard.assert_called_once_with(
        None, from_stdin=False, no_test=False, dialect_hint=None, manual=True
    )
    assert rc == 0


def test_cmd_add_manual_with_dialect_hint_both_propagate() -> None:
    """--manual and --dialect-hint together must both reach run_add_wizard."""
    mock_wizard = MagicMock(return_value=0)

    with patch("dbread.connstr.wizard.run_add_wizard", mock_wizard):
        rc = cmd_add(["myname", "--manual", "--dialect-hint", "postgres"])

    mock_wizard.assert_called_once_with(
        "myname", from_stdin=False, no_test=False, dialect_hint="postgres", manual=True
    )
    assert rc == 0
