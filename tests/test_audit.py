"""Tests for AuditLogger: JSONL format, rotation chain, fsync, tz, redact."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

from dbread.audit import AuditLogger


def test_log_single_record(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), rotate_mb=50)
    logger.log("conn1", "SELECT 1", status="ok", rows=1, ms=5)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["conn"] == "conn1"
    assert rec["sql"] == "SELECT 1"
    assert rec["status"] == "ok"
    assert rec["rows"] == 1
    assert "ts" in rec
    assert "reason" not in rec


def test_log_with_reason(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), rotate_mb=50)
    logger.log("c", "DELETE", status="rejected", reason="DML blocked")
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["status"] == "rejected"
    assert rec["reason"] == "DML blocked"


def test_multithread_write(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), rotate_mb=50)

    def worker(i: int) -> None:
        for k in range(10):
            logger.log(f"c{i}", f"SELECT {k}", status="ok", rows=1, ms=1)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    for line in lines:
        json.loads(line)


def test_rotation_triggers(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), rotate_mb=1)
    logger.rotate_bytes = 200

    for _ in range(50):
        logger.log("c", "SELECT " + ("x" * 20), status="ok", rows=1, ms=1)

    assert Path(str(path) + ".1").exists(), "rotation should produce .1 backup"


def test_rotation_without_initial_file(tmp_path: Path) -> None:
    path = tmp_path / "fresh.jsonl"
    logger = AuditLogger(str(path), rotate_mb=50)
    logger.log("c", "SELECT 1", status="ok")
    assert path.exists()


def test_fsync_called_per_write(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), rotate_mb=50)
    with patch("dbread.audit.os.fsync") as mock_fsync:
        logger.log("c", "SELECT 1", status="ok")
        assert mock_fsync.call_count == 1


def test_timezone_utc_default(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), rotate_mb=50)  # default UTC
    logger.log("c", "SELECT 1", status="ok")
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    ts: str = rec["ts"]
    assert ts.endswith("+00:00") or ts.endswith("Z"), f"expected UTC offset, got {ts}"


def test_timezone_custom(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), timezone="Asia/Bangkok")
    logger.log("c", "SELECT 1", status="ok")
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["ts"].endswith("+07:00")


def test_timezone_unknown_falls_back_to_utc(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), timezone="Not/A_Real_TZ")
    logger.log("c", "SELECT 1", status="ok")
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["ts"].endswith("+00:00")


def test_redact_literals_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), redact_literals=True)
    sql_in = "SELECT * FROM u WHERE name='secret' AND id=42"
    logger.log("c", sql_in, status="ok", dialect="postgres")
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    sql = rec["sql"]
    assert "'secret'" not in sql
    assert "42" not in sql
    assert "?" in sql


def test_redact_off_by_default(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path))
    sql_in = "SELECT * FROM u WHERE name='secret' AND id=42"
    logger.log("c", sql_in, status="ok", dialect="postgres")
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["sql"] == sql_in


def test_redact_parse_fail_fallback(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), redact_literals=True)
    bad = "not valid sql !!!"
    logger.log("c", bad, status="ok", dialect="postgres")
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["sql"] == bad  # fell back unredacted, no exception


def test_redact_without_dialect_skips(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), redact_literals=True)
    sql_in = "SELECT 'x'"
    logger.log("c", sql_in, status="ok")  # no dialect
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert rec["sql"] == sql_in


# ---- Mongo command redaction ---------------------------------------------


def test_redact_mongo_simple_filter() -> None:
    from dbread.audit import redact_mongo_command
    cmd = {"find": "users", "filter": {"email": "alice@x.com"}}
    out = redact_mongo_command(cmd)
    assert out == {"find": "users", "filter": {"email": "?"}}


def test_redact_mongo_operator_preserved() -> None:
    from dbread.audit import redact_mongo_command
    cmd = {"find": "u", "filter": {"age": {"$gt": 18}}}
    out = redact_mongo_command(cmd)
    assert out == {"find": "u", "filter": {"age": {"$gt": "?"}}}


def test_redact_mongo_meta_kept() -> None:
    from dbread.audit import redact_mongo_command
    cmd = {"find": "u", "filter": {"x": 1}, "maxTimeMS": 30000, "limit": 100, "skip": 10}
    out = redact_mongo_command(cmd)
    assert out["maxTimeMS"] == 30000
    assert out["limit"] == 100
    assert out["skip"] == 10


def test_redact_mongo_pipeline_match() -> None:
    from dbread.audit import redact_mongo_command
    cmd = {
        "aggregate": "u",
        "pipeline": [{"$match": {"email": "a@x"}}, {"$limit": 50}],
    }
    out = redact_mongo_command(cmd)
    assert out["pipeline"][0] == {"$match": {"email": "?"}}
    assert out["pipeline"][1] == {"$limit": 50}  # structural, kept


def test_redact_mongo_lookup_preserves_schema_keys() -> None:
    from dbread.audit import redact_mongo_command
    cmd = {
        "aggregate": "orders",
        "pipeline": [{"$lookup": {
            "from": "users",
            "localField": "user_id",
            "foreignField": "_id",
            "as": "u",
            "pipeline": [{"$match": {"email": "a@x"}}],
        }}],
    }
    out = redact_mongo_command(cmd)
    lookup = out["pipeline"][0]["$lookup"]
    assert lookup["from"] == "users"
    assert lookup["localField"] == "user_id"
    assert lookup["foreignField"] == "_id"
    assert lookup["as"] == "u"
    assert lookup["pipeline"][0] == {"$match": {"email": "?"}}


def test_redact_mongo_in_operator_list_values() -> None:
    from dbread.audit import redact_mongo_command
    cmd = {"find": "u", "filter": {"status": {"$in": ["active", "pending"]}}}
    out = redact_mongo_command(cmd)
    assert out["filter"]["status"]["$in"] == ["?", "?"]


def test_redact_mongo_noop_on_non_dict() -> None:
    from dbread.audit import redact_mongo_command
    assert redact_mongo_command({}) == {}


def test_rotation_three_chain(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), rotate_mb=1)
    logger.rotate_bytes = 1_000_000  # huge so _maybe_rotate never fires inside log()

    # Pre-populate 3 existing backups + current, all marker-tagged.
    path.write_text("CURRENT\n")
    (tmp_path / "a.jsonl.1").write_text("OLD1\n")
    (tmp_path / "a.jsonl.2").write_text("OLD2\n")
    (tmp_path / "a.jsonl.3").write_text("OLD3\n")

    # Force rotation directly by shrinking threshold and invoking rotation.
    logger.rotate_bytes = 1
    logger._maybe_rotate()

    assert (tmp_path / "a.jsonl.3").read_text() == "OLD2\n", "OLD3 dropped, OLD2 moved to .3"
    assert (tmp_path / "a.jsonl.2").read_text() == "OLD1\n", "OLD1 moved to .2"
    assert (tmp_path / "a.jsonl.1").read_text() == "CURRENT\n", "current moved to .1"
    assert not path.exists(), "current file should be moved"


def test_audit_path_expanduser(tmp_path: Path, monkeypatch) -> None:
    """AuditConfig.path expands ~ during pydantic validation."""
    from dbread.config import AuditConfig

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    cfg = AuditConfig(path="~/logs/audit.jsonl")
    assert "~" not in cfg.path
    assert cfg.path.endswith(os.path.join("logs", "audit.jsonl")) or cfg.path.endswith(
        "logs/audit.jsonl"
    )
