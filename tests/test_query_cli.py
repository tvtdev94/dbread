"""CLI tests for ``dbread query`` — uses a SQLite demo config in tmp_path."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from dbread import query_cli


def _seed_config(tmp_path: Path) -> Path:
    db = tmp_path / "sample.db"
    with sqlite3.connect(db) as con:
        con.executescript(
            "CREATE TABLE g (id INTEGER PRIMARY KEY, text TEXT);"
            "INSERT INTO g (text) VALUES ('alpha'),('beta'),('gamma,with,comma'),"
            " ('quoted \"x\"'),('unicode âêî');"
        )
    cfg = tmp_path / "config.yaml"
    audit = tmp_path / "audit.jsonl"
    cfg.write_text(
        "connections:\n"
        "  sample:\n"
        f"    url: sqlite:///{db}\n"
        "    dialect: sqlite\n"
        "    rate_limit_per_min: 60\n"
        "    statement_timeout_s: 5\n"
        "    max_rows: 100\n"
        "audit:\n"
        f"  path: {audit}\n"
        "  rotate_mb: 1\n",
        encoding="utf-8",
    )
    return cfg


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    cfg = _seed_config(tmp_path)
    monkeypatch.setenv("DBREAD_CONFIG", str(cfg))
    return cfg


def _run(
    args: list[str], capsys, *, isatty: bool = False, monkeypatch=None,
) -> tuple[int, str, str]:
    if monkeypatch is not None:
        monkeypatch.setattr(sys.stdout, "isatty", lambda: isatty)
    code = query_cli.main(args)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


# ---------------------------------------------------------------------------
# Arg parser
# ---------------------------------------------------------------------------


def test_help_flag_returns_zero_and_prints_usage(capsys) -> None:
    code, out, _ = _run(["--help"], capsys)
    assert code == 0
    assert "Usage: dbread query" in out


def test_missing_connection_returns_two(capsys) -> None:
    code, _, err = _run([], capsys)
    assert code == 2
    assert "connection name required" in err


def test_missing_sql_returns_two(capsys) -> None:
    code, _, err = _run(["sample"], capsys)
    assert code == 2
    assert "either SQL string or --command required" in err


def test_unknown_flag_returns_two(capsys) -> None:
    code, _, err = _run(["--no-audit", "sample", "SELECT 1"], capsys)
    assert code == 2
    assert "unknown flag" in err


def test_invalid_format_returns_two(capsys) -> None:
    code, _, err = _run(
        ["sample", "--format", "xml", "SELECT 1"], capsys
    )
    assert code == 2
    assert "--format must be one of" in err


def test_invalid_max_rows_returns_two(capsys) -> None:
    code, _, err = _run(["sample", "--max-rows", "abc", "SELECT 1"], capsys)
    assert code == 2


def test_invalid_command_json_returns_two(capsys) -> None:
    code, _, err = _run(["sample", "--command", "{not json"], capsys)
    assert code == 2
    assert "invalid JSON" in err


def test_missing_config_returns_four(capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DBREAD_CONFIG", str(tmp_path / "nope.yaml"))
    code, _, err = _run(["sample", "SELECT 1"], capsys)
    assert code == 4
    assert "config not found" in err


# ---------------------------------------------------------------------------
# Format dispatch
# ---------------------------------------------------------------------------


def test_jsonl_format_when_piped(cli_env, capsys, monkeypatch) -> None:
    code, out, _ = _run(["sample", "SELECT id, text FROM g ORDER BY id LIMIT 2"],
                        capsys, isatty=False, monkeypatch=monkeypatch)
    assert code == 0
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["id"] == 1 and rec["text"] == "alpha"


def test_table_format_when_tty(cli_env, capsys, monkeypatch) -> None:
    code, out, _ = _run(["sample", "SELECT id FROM g ORDER BY id LIMIT 2"],
                        capsys, isatty=True, monkeypatch=monkeypatch)
    assert code == 0
    assert "id" in out
    assert "─" in out  # separator
    assert "(2 rows)" in out


def test_csv_format_quotes_special_chars(cli_env, capsys, monkeypatch) -> None:
    code, out, _ = _run(
        ["sample", "--format", "csv", "SELECT id, text FROM g ORDER BY id LIMIT 5"],
        capsys, isatty=True, monkeypatch=monkeypatch,
    )
    assert code == 0
    lines = out.splitlines()
    assert lines[0] == "id,text"
    # comma in value must be quoted by csv.writer
    gamma = next(line for line in lines if "gamma" in line)
    assert '"gamma,with,comma"' in gamma
    # double-quote inside value must be escaped
    quoted = next(line for line in lines if "quoted" in line)
    assert '""x""' in quoted


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------


def test_guard_reject_returns_two(cli_env, capsys, monkeypatch) -> None:
    code, _, err = _run(["sample", "DELETE FROM g"], capsys, monkeypatch=monkeypatch)
    assert code == 2
    assert "sql_guard" in err


def test_unknown_connection_returns_four(cli_env, capsys, monkeypatch) -> None:
    code, _, err = _run(["nonexistent", "SELECT 1"], capsys, monkeypatch=monkeypatch)
    assert code == 4
    assert err  # some message


def test_explain_returns_zero_and_prints_plan(cli_env, capsys, monkeypatch) -> None:
    code, out, _ = _run(["sample", "--explain", "SELECT * FROM g"],
                        capsys, monkeypatch=monkeypatch)
    assert code == 0
    # JSON-pretty plan output
    parsed = json.loads(out)
    assert isinstance(parsed, list) and parsed  # at least one plan row


def test_max_rows_caps_returned_rows(cli_env, capsys, monkeypatch) -> None:
    code, out, _ = _run(
        ["sample", "--max-rows", "2", "SELECT * FROM g"],
        capsys, isatty=False, monkeypatch=monkeypatch,
    )
    assert code == 0
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 2


# ---------------------------------------------------------------------------
# Audit integrity
# ---------------------------------------------------------------------------


def test_query_writes_audit_record(cli_env, tmp_path, capsys, monkeypatch) -> None:
    _run(["sample", "SELECT 1 AS x"], capsys, monkeypatch=monkeypatch)
    audit_path = tmp_path / "audit.jsonl"
    assert audit_path.exists()
    rec = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["status"] == "ok"
    assert rec["conn"] == "sample"


# ---------------------------------------------------------------------------
# _render_table — focused unit
# ---------------------------------------------------------------------------


def test_render_table_truncates_long_cells(capsys) -> None:
    long_val = "x" * 80
    query_cli._render_table(["c"], [[long_val]])
    out = capsys.readouterr().out
    # truncated to 47 chars + ellipsis = 48 chars displayed
    line = out.splitlines()[2]  # header / sep / row
    assert line.rstrip().endswith("…")
    assert len(line.rstrip()) == 48


def test_render_table_empty_columns(capsys) -> None:
    query_cli._render_table([], [])
    assert "(no columns)" in capsys.readouterr().out
