"""Tests for AuditLogger JSONL format, rotation, and thread safety."""

from __future__ import annotations

import json
import threading
from pathlib import Path

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
    # every line parses as JSON -> no corruption
    for line in lines:
        json.loads(line)


def test_rotation_triggers(tmp_path: Path) -> None:
    path = tmp_path / "a.jsonl"
    logger = AuditLogger(str(path), rotate_mb=1)
    # rotate_bytes = 1MB -> force rotation with small limit via monkeypatching
    logger.rotate_bytes = 200

    # write until rotation happens
    for i in range(50):
        logger.log("c", "SELECT " + ("x" * 20), status="ok", rows=1, ms=1)

    backup = Path(str(path) + ".1")
    assert backup.exists(), "rotation should have produced .1 backup"


def test_rotation_without_initial_file(tmp_path: Path) -> None:
    path = tmp_path / "fresh.jsonl"
    logger = AuditLogger(str(path), rotate_mb=50)
    # should not raise when nothing to rotate
    logger.log("c", "SELECT 1", status="ok")
    assert path.exists()
