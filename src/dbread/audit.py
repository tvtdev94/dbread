"""Thread-safe JSONL audit logger.

Hardening in v0.2:
- fsync after each write -> record survives kill -9 / power loss.
- Configurable timezone (default UTC) via IANA name.
- 3-backup rotation chain (`.1` -> `.2` -> `.3`) for a wider forensic window.
- Opt-in PII redaction of SQL literals using sqlglot AST rewrite.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import sqlglot
from sqlglot import exp

log = logging.getLogger("dbread.audit")


# Top-level and pipeline-stage meta fields whose numeric values are debug
# info, not PII — keep them visible in redacted audit logs.
_META_FIELDS = frozenset({
    "maxTimeMS", "limit", "skip", "batchSize", "hint", "collation",
    "comment", "readConcern", "singleBatch",
})

# Stage keys whose scalar value is structural, not PII.
_STRUCTURAL_SCALAR_STAGES = frozenset({"$limit", "$skip", "$count", "$sample"})

# Inside a `$lookup` / `$graphLookup` stage value, these keys identify
# collections or fields (schema metadata) — keep them, not PII.
_LOOKUP_SCHEMA_KEYS = frozenset({
    "from", "as", "localField", "foreignField",
    "startWith", "connectFromField", "connectToField",
    "depthField", "maxDepth",
})


def redact_mongo_command(cmd: dict) -> dict:
    """Replace scalar leaf values with '?' while keeping keys + structure.

    Meta fields (maxTimeMS, limit, skip, ...) keep their numeric values so an
    operator can still debug a slow query. Collection names (command first
    value) are preserved. $lookup schema keys (from/localField/...) kept.
    """
    if not isinstance(cmd, dict) or not cmd:
        return cmd
    out: dict = {}
    items = list(cmd.items())
    for i, (k, v) in enumerate(items):
        if i == 0:
            # first key is the command name → its value is the collection name
            out[k] = v
            continue
        if k in _META_FIELDS:
            out[k] = v
            continue
        out[k] = _redact_value(v)
    return out


def _redact_value(value: Any, parent_stage: str | None = None) -> Any:
    if isinstance(value, dict):
        return {k: _redact_inner(k, v, parent_stage) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, parent_stage) for item in value]
    return "?"


def _redact_inner(key: str, value: Any, parent_stage: str | None) -> Any:
    if key in _META_FIELDS:
        return value
    if parent_stage in ("$lookup", "$graphLookup") and key in _LOOKUP_SCHEMA_KEYS:
        return value
    if key in _STRUCTURAL_SCALAR_STAGES and not isinstance(value, (dict, list)):
        return value
    if key.startswith("$"):
        if isinstance(value, (dict, list)):
            return _redact_value(value, parent_stage=key)
        return "?"
    # regular field name → walk value (keep operator KEYS, redact leaf VALUES)
    if isinstance(value, (dict, list)):
        return _redact_value(value, parent_stage)
    return "?"


def _resolve_tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning("unknown timezone %r; falling back to UTC", name)
        return ZoneInfo("UTC")


# Minimum gap between opportunistic prune passes during `log()` calls.
# Keeps the per-call overhead effectively zero while still catching drift
# on long-running MCP servers that never hit the size rotation threshold.
_PRUNE_INTERVAL_S = 3600.0


class AuditLogger:
    def __init__(
        self,
        path: str,
        rotate_mb: int = 50,
        timezone: str = "UTC",
        redact_literals: bool = False,
        retention_days: int | None = None,
    ) -> None:
        self.path = path
        self.rotate_bytes = max(1, rotate_mb) * 1024 * 1024
        self.redact_literals = redact_literals
        self.retention_days = retention_days
        self._tz = _resolve_tz(timezone)
        self._lock = threading.Lock()
        self._last_prune_ts = 0.0
        # One-shot prune at startup — handles stale backups accumulated while
        # the server was stopped. Cheap when files are small or absent.
        if retention_days:
            with self._lock:
                self._prune_old_entries()

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
            self._maybe_prune()
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

    def _maybe_prune(self) -> None:
        """Run a prune pass at most once per hour; no-op when retention unset."""
        if not self.retention_days:
            return
        now = time.monotonic()
        if now - self._last_prune_ts < _PRUNE_INTERVAL_S:
            return
        self._prune_old_entries()

    def _prune_old_entries(self) -> None:
        """Rewrite audit files keeping only entries newer than `retention_days`.

        Applies to current file + all rotated backups. Malformed lines are
        kept (fail-safe: never silently drop data we can't parse).
        Caller is responsible for holding `self._lock`.
        """
        if not self.retention_days:
            return
        cutoff = datetime.now(self._tz) - timedelta(days=self.retention_days)
        for path in (self.path, f"{self.path}.1", f"{self.path}.2", f"{self.path}.3"):
            if os.path.exists(path):
                _rewrite_newer_than(path, cutoff)
        self._last_prune_ts = time.monotonic()


def _rewrite_newer_than(path: str, cutoff: datetime) -> None:
    """Atomic rewrite: keep lines whose `ts` is >= cutoff (or unparseable)."""
    tmp = f"{path}.tmp"
    try:
        with open(path, encoding="utf-8") as src:
            kept = [line for line in src if _entry_newer_than(line, cutoff)]
        with open(tmp, "w", encoding="utf-8") as dst:
            dst.writelines(kept)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp, path)
    except OSError as e:
        log.warning("prune: rewrite %s failed: %s", path, e)
        with contextlib.suppress(OSError):
            os.remove(tmp)


def _entry_newer_than(line: str, cutoff: datetime) -> bool:
    """Return True for entries to keep. Fail-safe: malformed lines kept."""
    try:
        rec = json.loads(line)
        ts = datetime.fromisoformat(rec["ts"])
        return ts >= cutoff
    except (json.JSONDecodeError, ValueError, KeyError, TypeError):
        return True
