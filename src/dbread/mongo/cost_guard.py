"""MongoDB cost guard — flags COLLSCAN plans whose collection exceeds threshold.

Strategy:
- Run ``db.command({explain: <cmd>, verbosity: "queryPlanner"})`` (plan-only —
  ``executionStats``/``allPlansExecution`` would run the query, breaking the
  "no side effects" contract).
- Walk the winning plan iteratively (BFS, depth-bounded) for any ``COLLSCAN``
  node. If found, the cost = ``collStats.count`` of the target collection
  (worst case: full scan). All-IXSCAN plans return ``None`` (fail-open) since
  ``queryPlanner`` does not expose index selectivity.

This is intentionally conservative — false-negative friendly (lets indexed
queries through) but catches the primary DoS vector (full-collection sweeps).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

log = logging.getLogger("dbread.mongo.cost_guard")

_MAX_WALK_NODES = 2000  # hard cap on plan walker — prevents pathological plans


class MongoCostGuard:
    """Estimate worst-case docs scanned for a Mongo command."""

    def estimate_docs(
        self,
        db: Any,
        command: dict,
        threshold: int | None,
    ) -> tuple[int | None, int]:
        """Return ``(docs_estimate, cost_check_ms)`` — fail-open on any error."""
        if threshold is None:
            return None, 0
        t0 = time.perf_counter()
        try:
            docs = self._estimate(db, command)
        except Exception as e:
            log.debug("mongo cost guard failed: %s", e)
            docs = None
        ms = int((time.perf_counter() - t0) * 1000)
        return docs, ms

    def _estimate(self, db: Any, command: dict) -> int | None:
        plan = db.command("explain", command, verbosity="queryPlanner")
        if not isinstance(plan, dict):
            return None
        if not _has_collscan(plan):
            return None  # all-index plans → no signal, fail-open
        coll_name = _extract_collection(plan, command)
        if not coll_name:
            return None
        stats = db.command("collStats", coll_name)
        n = stats.get("count") if isinstance(stats, dict) else None
        if n is None:
            return None
        try:
            return int(n)
        except (TypeError, ValueError):
            return None


def _has_collscan(plan: Any) -> bool:
    """Iterative BFS for any node with ``stage == "COLLSCAN"``."""
    seen: set[int] = set()
    queue: deque[Any] = deque([plan])
    visited = 0
    while queue and visited < _MAX_WALK_NODES:
        node = queue.popleft()
        visited += 1
        if isinstance(node, dict):
            if id(node) in seen:
                continue
            seen.add(id(node))
            if node.get("stage") == "COLLSCAN":
                return True
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return False


def _extract_collection(plan: Any, command: dict) -> str | None:
    """Locate ``queryPlanner.namespace = 'db.coll'`` then strip db prefix.

    Falls back to the command's first-key value (Mongo command convention:
    ``{find: "<coll>", ...}`` / ``{aggregate: "<coll>", ...}``).
    """
    seen: set[int] = set()
    queue: deque[Any] = deque([plan])
    visited = 0
    while queue and visited < _MAX_WALK_NODES:
        node = queue.popleft()
        visited += 1
        if isinstance(node, dict):
            if id(node) in seen:
                continue
            seen.add(id(node))
            ns = node.get("namespace")
            if isinstance(ns, str) and "." in ns:
                return ns.split(".", 1)[1]
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    first_key = next(iter(command), None)
    if first_key:
        v = command[first_key]
        if isinstance(v, str):
            return v
    return None
