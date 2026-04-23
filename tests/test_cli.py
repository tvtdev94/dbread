"""Tests for the `dbread init` subcommand and help / version dispatch."""

from __future__ import annotations

import os
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


# ---- install-skill: Claude Code integration -------------------------------


def test_install_skill_skips_when_claude_dir_missing(fake_home: Path, capsys) -> None:
    """No ~/.claude -> silent no-op (user doesn't have Claude Code)."""
    from dbread.cli import install_skill

    rc = install_skill()
    assert rc == 0
    out = capsys.readouterr().out
    assert "~/.claude not found" in out
    assert not (fake_home / ".claude").exists()


def test_install_skill_creates_file_when_claude_dir_exists(
    fake_home: Path, capsys
) -> None:
    from dbread.cli import install_skill

    (fake_home / ".claude").mkdir()

    rc = install_skill()
    assert rc == 0
    target = fake_home / ".claude" / "skills" / "dbread" / "SKILL.md"
    assert target.is_file()
    content = target.read_text(encoding="utf-8")
    assert content.startswith("---")
    assert "name: dbread" in content
    assert "description:" in content


def test_install_skill_idempotent_without_force(fake_home: Path, capsys) -> None:
    """Second run without --force skips to avoid clobbering user edits."""
    from dbread.cli import install_skill

    (fake_home / ".claude").mkdir()

    install_skill()
    target = fake_home / ".claude" / "skills" / "dbread" / "SKILL.md"
    target.write_text("USER-EDITED", encoding="utf-8")

    install_skill()  # no force
    assert target.read_text(encoding="utf-8") == "USER-EDITED"
    out = capsys.readouterr().out
    assert "already exists" in out


def test_install_skill_force_overwrites(fake_home: Path) -> None:
    from dbread.cli import install_skill

    (fake_home / ".claude").mkdir()
    install_skill()
    target = fake_home / ".claude" / "skills" / "dbread" / "SKILL.md"
    target.write_text("STALE", encoding="utf-8")

    install_skill(force=True)
    content = target.read_text(encoding="utf-8")
    assert content != "STALE"
    assert "name: dbread" in content


def test_install_skill_quiet_suppresses_skip_message(
    fake_home: Path, capsys
) -> None:
    from dbread.cli import install_skill

    install_skill(quiet=True)
    out = capsys.readouterr().out
    assert out == ""


def test_init_triggers_skill_install_when_claude_present(
    fake_home: Path, capsys
) -> None:
    (fake_home / ".claude").mkdir()

    init_config()
    out = capsys.readouterr().out
    skill_path = fake_home / ".claude" / "skills" / "dbread" / "SKILL.md"
    assert skill_path.is_file()
    assert str(skill_path) in out


def test_init_skips_skill_install_when_claude_missing(
    fake_home: Path, capsys
) -> None:
    """init still works fine on machines without Claude Code."""
    init_config()
    assert not (fake_home / ".claude").exists()
    assert (fake_home / ".dbread" / "config.yaml").is_file()


def test_install_skill_subcommand_via_cli(fake_home: Path) -> None:
    (fake_home / ".claude").mkdir()
    proc = subprocess.run(
        [sys.executable, "-m", "dbread.server", "install-skill"],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "HOME": str(fake_home), "USERPROFILE": str(fake_home)},
    )
    assert proc.returncode == 0
    assert (fake_home / ".claude" / "skills" / "dbread" / "SKILL.md").is_file()


def test_install_skill_force_subcommand_via_cli(fake_home: Path) -> None:
    (fake_home / ".claude").mkdir()
    target = fake_home / ".claude" / "skills" / "dbread" / "SKILL.md"
    target.parent.mkdir(parents=True)
    target.write_text("STALE-SUBPROC", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "dbread.server", "install-skill", "--force"],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "HOME": str(fake_home), "USERPROFILE": str(fake_home)},
    )
    assert proc.returncode == 0
    assert target.read_text(encoding="utf-8") != "STALE-SUBPROC"
    assert "name: dbread" in target.read_text(encoding="utf-8")
