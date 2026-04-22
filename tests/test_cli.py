"""Tests for the `dbread init` subcommand and help / version dispatch."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from dbread.cli import init_config


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def test_init_creates_all_three_files(fake_home: Path, capsys) -> None:
    rc = init_config()
    assert rc == 0
    d = fake_home / ".dbread"
    assert (d / "config.yaml").is_file()
    assert (d / ".env").is_file()
    assert (d / "sample.db").is_file()

    out = capsys.readouterr().out
    assert "claude mcp add" in out
    assert str(d / "config.yaml") in out


def test_generated_config_parses_as_settings(fake_home: Path) -> None:
    init_config()
    from dbread.config import Settings

    cfg_path = fake_home / ".dbread" / "config.yaml"
    settings = Settings.load(cfg_path)
    assert "sample" in settings.connections
    assert settings.connections["sample"].dialect == "sqlite"
    assert settings.audit.timezone == "UTC"


def test_sample_db_has_demo_data(fake_home: Path) -> None:
    init_config()
    import sqlite3

    db = fake_home / ".dbread" / "sample.db"
    with sqlite3.connect(db) as con:
        rows = con.execute("SELECT count(*) FROM greetings").fetchone()
    assert rows[0] >= 3


def test_init_idempotent_skips_existing(fake_home: Path, capsys) -> None:
    init_config()
    capsys.readouterr()  # discard first output
    rc = init_config()
    assert rc == 0
    out = capsys.readouterr().out
    assert out.count("skipped") >= 3
    assert "already exists" in out


def test_version_subcommand(fake_home: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "dbread.server", "--version"],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    assert "dbread" in proc.stdout
    assert proc.stdout.strip().split()[-1].startswith("0.")


def test_help_subcommand(fake_home: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "dbread.server", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    assert "dbread init" in proc.stdout


def test_unknown_arg_exits_nonzero(fake_home: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "dbread.server", "--bogus"],
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode != 0
    assert "unknown argument" in proc.stderr


def test_generated_url_uses_absolute_path(fake_home: Path) -> None:
    init_config()
    cfg = yaml.safe_load((fake_home / ".dbread" / "config.yaml").read_text())
    url = cfg["connections"]["sample"]["url"]
    # must NOT contain ~ (SQLite URI does not expand tilde)
    assert "~" not in url
    assert str(fake_home).replace("\\", "/") in url or str(fake_home) in url
