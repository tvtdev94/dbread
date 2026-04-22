"""Thread-safe JSONL audit logger.

Hardening in v0.2:
- fsync after each write -> record survives kill -9 / power loss.
- Configurable timezone (default UTC) via IANA name.
- 3-backup rotation chain (`.1` -> `.2` -> `.3`) for a wider forensic window.
- Opt-in PII redaction of SQL literals using sqlglot AST rewrite.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlglot
from sqlglot import exp

log = logging.getLogger("dbread.audit")


def _resolve_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning("unknown timezone %r; falling back to UTC", name)
        return ZoneInfo("UTC")


class AuditLogger:
    def __init__(
        self,
        path: str,
        rotate_mb: int = 50,
        timezone: str = "UTC",
        redact_literals: bool = False,
    ) -> None:
        self.path = path
        self.rotate_bytes = max(1, rotate_mb) * 1024 * 1024
        self.redact_literals = redact_literals
        self._tz = _resolve_tz(timezone)
        self._lock = threading.Lock()

    def log(
        self,
        conn: str,
        sql: str,
        status: str,
        rows: int = 0,
        ms: int = 0,
        reason: str | None = None,
        dialect: str | None = None,
    ) -> None:
        logged_sql = self._redact(sql, dialect) if self.redact_literals else sql
        record: dict[str, object] = {
            "ts": datetime.now(self._tz).isoformat(timespec="seconds"),
            "conn": conn,
            "sql": logged_sql,
            "rows": rows,
            "ms": ms,
            "status": status,
        }
        if reason:
            record["reason"] = reason
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            self._maybe_rotate()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

    def _redact(self, sql: str, dialect: str | None) -> str:
        if not dialect:
            return sql
        try:
            ast = sqlglot.parse_one(sql, read=dialect)
            if ast is None:
                return sql
            for lit in ast.find_all(exp.Literal):
                lit.replace(exp.Placeholder(this="?"))
            return ast.sql(dialect=dialect)
        except Exception:
            return sql

    def _maybe_rotate(self) -> None:
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            return
        if size < self.rotate_bytes:
            return
        # chain: current -> .1 -> .2 -> .3 (oldest discarded)
        tail = f"{self.path}.3"
        if os.path.exists(tail):
            try:
                os.remove(tail)
            except OSError as e:
                log.warning("rotate: failed to remove %s: %s", tail, e)
                return
        for i in (3, 2, 1):
            src = self.path if i == 1 else f"{self.path}.{i - 1}"
            dst = f"{self.path}.{i}"
            if os.path.exists(src):
                try:
                    os.replace(src, dst)
                except OSError as e:
                    log.warning("rotate: %s -> %s failed: %s", src, dst, e)
                    return
