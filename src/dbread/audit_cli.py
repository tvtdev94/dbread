"""`dbread audit` — on-demand analysis of audit.jsonl (+ rotated backups).

Subcommands (flags):
  (no flags)          → summary (counts, top rejected, top slow)
  --since 1h|30m|7d   → filter entries newer than duration
  --conn NAME         → filter by connection
  --slow MS           → list queries exceeding MS (sorted desc)
  --rejected          → only rejections, grouped by reason
  --tail              → follow mode (like tail -f); survives rotation best-effort

Kept dependency-free (no tabulate/rich) to avoid extra runtime footprint.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_DUR_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_UNIT_SECS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(s: str) -> timedelta:
    m = _DUR_RE.match(s)
    if not m:
        raise ValueError(f"bad duration {s!r}; expected e.g. 1h, 30m, 7d")
    return timedelta(seconds=int(m.group(1)) * _UNIT_SECS[m.group(2).lower()])


def _rotated_paths(base: str) -> list[str]:
    """Return audit files oldest-first (.3, .2, .1, base) that exist."""
    paths = [f"{base}.3", f"{base}.2", f"{base}.1", base]
    return [p for p in paths if os.path.exists(p)]


def _parse_ts(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def _iter_entries(
    base: str,
    *,
    since: timedelta | None = None,
    conn: str | None = None,
) -> Iterator[dict[str, Any]]:
    cutoff = datetime.now(UTC) - since if since else None
    for path in _rotated_paths(base):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if conn and rec.get("conn") != conn:
                        continue
                    if cutoff is not None:
                        ts = _parse_ts(rec.get("ts", ""))
                        if ts is None:
                            continue
                        # Normalize to aware UTC for comparison.
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=UTC)
                        if ts < cutoff:
                            continue
                    yield rec
        except OSError:
            continue


def _summarize(entries: list[dict[str, Any]]) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    conn_counts: dict[str, int] = {}
    slow: list[tuple[int, str, str]] = []  # (ms, conn, sql)
    total_ms = 0
    ok_count = 0

    for rec in entries:
        status = rec.get("status", "?")
        status_counts[status] = status_counts.get(status, 0) + 1
        c = rec.get("conn", "?")
        conn_counts[c] = conn_counts.get(c, 0) + 1
        ms = int(rec.get("ms") or 0)
        if status == "ok":
            ok_count += 1
            total_ms += ms
        if status == "rejected":
            r = rec.get("reason", "unknown")
            reason_counts[r] = reason_counts.get(r, 0) + 1
        if ms > 0:
            slow.append((ms, c, rec.get("sql", "")))
    slow.sort(reverse=True)
    return {
        "total": len(entries),
        "status": status_counts,
        "conn": conn_counts,
        "rejected_reasons": reason_counts,
        "top_slow": slow[:10],
        "avg_ok_ms": (total_ms // ok_count) if ok_count else 0,
    }


def _trunc(s: str, n: int = 80) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "..."


def _fmt_summary(summary: dict[str, Any]) -> str:
    out: list[str] = []
    out.append(f"Total entries: {summary['total']}")
    out.append("")
    out.append("Status:")
    for s, n in sorted(summary["status"].items(), key=lambda x: -x[1]):
        out.append(f"  {s:<12} {n}")
    out.append("")
    out.append("By connection:")
    for c, n in sorted(summary["conn"].items(), key=lambda x: -x[1]):
        out.append(f"  {c:<20} {n}")
    if summary["rejected_reasons"]:
        out.append("")
        out.append("Rejection reasons:")
        for r, n in sorted(summary["rejected_reasons"].items(), key=lambda x: -x[1]):
            out.append(f"  {n:>5}  {r}")
    if summary["top_slow"]:
        out.append("")
        out.append(f"Top slow (avg ok={summary['avg_ok_ms']} ms):")
        for ms, c, sql in summary["top_slow"]:
            out.append(f"  {ms:>6} ms  [{c}]  {_trunc(sql, 70)}")
    return "\n".join(out)


def _fmt_rejected(entries: list[dict[str, Any]]) -> str:
    groups: dict[str, list[dict[str, Any]]] = {}
    for rec in entries:
        if rec.get("status") != "rejected":
            continue
        groups.setdefault(rec.get("reason", "unknown"), []).append(rec)
    if not groups:
        return "No rejections found."
    out: list[str] = []
    for reason, recs in sorted(groups.items(), key=lambda x: -len(x[1])):
        out.append(f"[{len(recs)}] {reason}")
        for r in recs[:5]:
            ts = r.get("ts", "?")
            c = r.get("conn", "?")
            out.append(f"    {ts}  [{c}]  {_trunc(r.get('sql',''), 70)}")
        if len(recs) > 5:
            out.append(f"    ... +{len(recs) - 5} more")
    return "\n".join(out)


def _fmt_slow(entries: list[dict[str, Any]], threshold_ms: int) -> str:
    rows = sorted(
        (r for r in entries if int(r.get("ms") or 0) >= threshold_ms),
        key=lambda r: -int(r.get("ms") or 0),
    )
    if not rows:
        return f"No entries >= {threshold_ms} ms."
    out = [f"{len(rows)} entries >= {threshold_ms} ms:"]
    for r in rows[:20]:
        out.append(
            f"  {int(r.get('ms') or 0):>6} ms  {r.get('ts','?')}  "
            f"[{r.get('conn','?')}]  {_trunc(r.get('sql',''), 70)}"
        )
    if len(rows) > 20:
        out.append(f"  ... +{len(rows) - 20} more")
    return "\n".join(out)


def _tail(base: str, conn: str | None) -> int:
    """Follow mode: print new lines as they append. Best-effort on rotation."""
    path = base
    pos = os.path.getsize(path) if os.path.exists(path) else 0
    while True:
        try:
            size = os.path.getsize(path)
        except FileNotFoundError:
            time.sleep(1)
            continue
        if size < pos:
            pos = 0  # rotated/truncated
        if size > pos:
            with open(path, encoding="utf-8") as f:
                f.seek(pos)
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if conn and rec.get("conn") != conn:
                        continue
                    sys.stdout.write(line if line.endswith("\n") else line + "\n")
                    sys.stdout.flush()
                pos = f.tell()
        time.sleep(1)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dbread audit", description="Analyze audit.jsonl")
    p.add_argument("--path", default=None, help="override audit.jsonl path")
    p.add_argument("--since", help="e.g. 1h, 30m, 7d")
    p.add_argument("--conn", help="filter by connection name")
    p.add_argument("--slow", type=int, help="list queries >= MS")
    p.add_argument("--rejected", action="store_true", help="show only rejections grouped")
    p.add_argument("--tail", action="store_true", help="follow new entries")
    args = p.parse_args(argv)

    base = args.path or _default_audit_path()
    if args.tail:
        try:
            return _tail(base, args.conn)
        except KeyboardInterrupt:
            return 0

    since = _parse_duration(args.since) if args.since else None
    entries = list(_iter_entries(base, since=since, conn=args.conn))
    if not entries:
        print(f"No audit entries found at {base}" + (" (filtered)" if (since or args.conn) else ""))
        return 0

    if args.rejected:
        print(_fmt_rejected(entries))
    elif args.slow is not None:
        print(_fmt_slow(entries, args.slow))
    else:
        print(_fmt_summary(_summarize(entries)))
    return 0


def _default_audit_path() -> str:
    """Resolve audit path: env override → config → fallback."""
    env = os.environ.get("DBREAD_AUDIT_PATH")
    if env:
        return str(Path(env).expanduser())
    cfg = os.environ.get("DBREAD_CONFIG")
    if cfg:
        try:
            from .config import Settings

            s = Settings.load(cfg)
            return s.audit.path
        except Exception:
            pass
    return str(Path("~/.dbread/audit.jsonl").expanduser())
