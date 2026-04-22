"""Thread-safe JSONL audit logger with size-based rotation."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone

_TZ = timezone(timedelta(hours=7))


class AuditLogger:
    def __init__(self, path: str, rotate_mb: int = 50) -> None:
        self.path = path
        self.rotate_bytes = max(1, rotate_mb) * 1024 * 1024
        self._lock = threading.Lock()

    def log(
        self,
        conn: str,
        sql: str,
        status: str,
        rows: int = 0,
        ms: int = 0,
        reason: str | None = None,
    ) -> None:
        record: dict[str, object] = {
            "ts": datetime.now(_TZ).isoformat(timespec="seconds"),
            "conn": conn,
            "sql": sql,
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

    def _maybe_rotate(self) -> None:
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            return
        if size < self.rotate_bytes:
            return
        backup = self.path + ".1"
        if os.path.exists(backup):
            os.remove(backup)
        os.rename(self.path, backup)
