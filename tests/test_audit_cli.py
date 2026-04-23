"""Tests for dbread audit CLI — summary, filters, rotation aggregation."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dbread.audit_cli import (
    _iter_entries,
    _parse_duration,
    _summarize,
    main,
)


def _write(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _rec(
    *,
    ts: str | None = None,
    conn: str = "a",
    sql: str = "SELECT 1",
    status: str = "ok",
    ms: int = 5,
    reason: str | None = None,
) -> dict[str, object]:
    if ts is None:
        ts = datetime.now(UTC).isoformat(timespec="seconds")
    out: dict[str, object] = {
        "ts": ts, "conn": conn, "sql": sql, "rows": 1, "ms": ms, "status": status,
    }
    if reason:
        out["reason"] = reason
    return out


def test_parse_duration_units() -> None:
    assert _parse_duration("30s") == timedelta(seconds=30)
    assert _parse_duration("10m") == timedelta(minutes=10)
    assert _parse_duration("2h") == timedelta(hours=2)
    assert _parse_duration("7d") == timedelta(days=7)
    with pytest.raises(ValueError):
        _parse_duration("banana")


def test_iter_entries_empty(tmp_path: Path) -> None:
    entries = list(_iter_entries(str(tmp_path / "nope.jsonl")))
    assert entries == []


def test_summary_counts(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    _write(p, [
        _rec(status="ok", ms=10),
        _rec(status="ok", ms=100),
        _rec(status="rejected", reason="node_rejected: Delete", ms=0),
        _rec(status="rejected", reason="rate_limit_connection", ms=0),
        _rec(status="failed", ms=50, reason="oops"),
    ])
    entries = list(_iter_entries(str(p)))
    s = _summarize(entries)
    assert s["total"] == 5
    assert s["status"]["ok"] == 2
    assert s["status"]["rejected"] == 2
    assert s["status"]["failed"] == 1
    assert s["rejected_reasons"]["node_rejected: Delete"] == 1
    assert s["rejected_reasons"]["rate_limit_connection"] == 1
    assert s["top_slow"][0][0] == 100


def test_since_filter(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    old = (datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds")
    new = datetime.now(UTC).isoformat(timespec="seconds")
    _write(p, [_rec(ts=old, sql="OLD"), _rec(ts=new, sql="NEW")])
    entries = list(_iter_entries(str(p), since=timedelta(hours=1)))
    sqls = [e["sql"] for e in entries]
    assert sqls == ["NEW"]


def test_conn_filter(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    _write(p, [_rec(conn="a"), _rec(conn="b"), _rec(conn="a")])
    entries = list(_iter_entries(str(p), conn="a"))
    assert len(entries) == 2
    assert all(e["conn"] == "a" for e in entries)


def test_rotated_files_aggregated(tmp_path: Path) -> None:
    base = tmp_path / "audit.jsonl"
    _write(Path(str(base) + ".3"), [_rec(sql="oldest")])
    _write(Path(str(base) + ".2"), [_rec(sql="older")])
    _write(Path(str(base) + ".1"), [_rec(sql="old")])
    _write(base, [_rec(sql="current")])
    entries = list(_iter_entries(str(base)))
    sqls = [e["sql"] for e in entries]
    assert sqls == ["oldest", "older", "old", "current"]


def test_malformed_lines_skipped(tmp_path: Path) -> None:
    p = tmp_path / "a.jsonl"
    p.write_text(
        json.dumps(_rec(sql="ok1")) + "\n"
        "not-json\n"
        "\n"
        + json.dumps(_rec(sql="ok2")) + "\n",
        encoding="utf-8",
    )
    entries = list(_iter_entries(str(p)))
    assert len(entries) == 2


def test_main_summary_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "audit.jsonl"
    _write(p, [_rec(status="ok"), _rec(status="rejected", reason="bad", ms=0)])
    rc = main(["--path", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Total entries: 2" in out
    assert "Rejection reasons" in out


def test_main_rejected_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "audit.jsonl"
    _write(p, [
        _rec(status="rejected", reason="node_rejected: Delete"),
        _rec(status="rejected", reason="node_rejected: Delete"),
        _rec(status="ok"),
    ])
    rc = main(["--path", str(p), "--rejected"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[2] node_rejected: Delete" in out


def test_main_slow_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "audit.jsonl"
    _write(p, [_rec(ms=50), _rec(ms=1500), _rec(ms=2500)])
    rc = main(["--path", str(p), "--slow", "1000"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 entries >= 1000 ms" in out
    assert "2500 ms" in out


def test_main_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    p = tmp_path / "missing.jsonl"
    rc = main(["--path", str(p)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No audit entries" in out
