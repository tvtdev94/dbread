"""Layer-1 validator for MongoDB commands and aggregation pipelines.

Allowlist-based: unknown commands/stages are rejected (default-deny). Walks
nested sub-pipelines in `$facet`, `$lookup.pipeline` so write stages cannot
hide inside legitimate shapes. Also scans all dict keys for operators whose
mere presence indicates JS execution (`$function`, `$accumulator`, `$where`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ALLOWED_COMMANDS = frozenset({
    "find", "count", "countDocuments", "estimatedDocumentCount",
    "distinct", "aggregate",
})

# Per-command top-level field allowlist. Anything outside these sets is
# rejected rather than silently ignored. Adding a field here means teaching
# the executor to apply it and covering it with a behavioral test — the
# equality check in `tests/test_mongo_guard.py` only catches the declarations
# drifting apart, not a field that is declared and then never used.
COMMAND_FIELDS: dict[str, frozenset[str]] = {
    "find": frozenset({
        "find", "filter", "projection", "sort", "skip", "limit",
        "hint", "collation", "batchSize", "comment", "maxTimeMS",
    }),
    "aggregate": frozenset({
        "aggregate", "pipeline", "collation", "let", "hint",
        "allowDiskUse", "comment", "maxTimeMS",
    }),
    "count": frozenset({
        "count", "filter", "skip", "limit", "hint", "collation",
        "comment", "maxTimeMS",
    }),
    "countDocuments": frozenset({
        "countDocuments", "filter", "skip", "limit", "hint", "collation",
        "comment", "maxTimeMS",
    }),
    "estimatedDocumentCount": frozenset({
        "estimatedDocumentCount", "comment", "maxTimeMS",
    }),
    "distinct": frozenset({
        "distinct", "key", "filter", "collation", "comment", "maxTimeMS",
    }),
}

ALLOWED_STAGES = frozenset({
    "$match", "$project", "$group", "$sort", "$limit", "$skip",
    "$count", "$facet", "$bucket", "$bucketAuto", "$unwind",
    "$addFields", "$set", "$unset", "$replaceRoot", "$replaceWith",
    "$sortByCount", "$densify", "$fill", "$lookup",
    "$redact", "$sample", "$graphLookup", "$setWindowFields",
    "$geoNear", "$unionWith",
    # Metadata reads. $collStats works under the plain `read` role;
    # $indexStats additionally needs the `indexStats` action, and says so
    # loudly when the role lacks it — see docs/setup-db-readonly.md.
    "$collStats", "$indexStats",
})

BLOCKED_STAGES = frozenset({
    "$out", "$merge",
    "$function", "$accumulator",
})

BLOCKED_OPERATORS_ANYWHERE = frozenset({
    "$function", "$accumulator", "$where", "$out", "$merge",
})

MAX_PIPELINE_DEPTH = 10


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    reason: str | None = None


_ALLOW = GuardResult(allowed=True)


class MongoGuard:
    """Validates MongoDB command dicts against the read-only allowlist."""

    def validate_command(self, cmd: Any) -> GuardResult:
        if not isinstance(cmd, dict) or not cmd:
            return GuardResult(False, "empty_command")

        name = next(iter(cmd))
        if name not in ALLOWED_COMMANDS:
            return GuardResult(False, f"command_not_allowed: {name}")

        unknown = set(cmd) - COMMAND_FIELDS[name]
        if unknown:
            return GuardResult(False, f"field_not_allowed: {sorted(unknown)[0]}")

        res = _check_field_types(cmd)
        if not res.allowed:
            return res

        # Scan entire command for JS-exec / write operators at any depth.
        blocked = _find_blocked_operator(cmd)
        if blocked is not None:
            return GuardResult(False, f"blocked_operator: {blocked}")

        if name == "aggregate":
            pipeline = cmd.get("pipeline")
            if not isinstance(pipeline, list):
                return GuardResult(False, "aggregate_missing_pipeline")
            res = _validate_pipeline(pipeline, depth=0)
            if not res.allowed:
                return res

        return _ALLOW

    def inject_limit(self, cmd: dict, cap: int) -> dict:
        """Clamp result size for find/aggregate to <= cap."""
        out = dict(cmd)
        name = next(iter(out))
        if name == "find":
            current = out.get("limit")
            if not isinstance(current, int) or current <= 0 or current > cap:
                out["limit"] = cap
        elif name == "aggregate":
            pipeline = list(out.get("pipeline", []))
            if pipeline and isinstance(pipeline[-1], dict) and "$limit" in pipeline[-1]:
                existing = pipeline[-1]["$limit"]
                if isinstance(existing, int) and existing > 0:
                    pipeline[-1] = {"$limit": min(existing, cap)}
                else:
                    pipeline[-1] = {"$limit": cap}
            else:
                pipeline.append({"$limit": cap})
            out["pipeline"] = pipeline
        return out


_DICT_FIELDS = ("filter", "projection", "sort", "collation", "let")
_NON_NEGATIVE_INT_FIELDS = ("skip", "limit", "batchSize")


def _check_field_types(cmd: dict) -> GuardResult:
    """Cheap shape checks so bad input fails here with a clear reason.

    Deliberately shallow: semantic validation (unknown index for `hint`,
    nonsense sort direction) belongs to the server, not to a second query
    planner living in the guard.
    """
    for field in _DICT_FIELDS:
        if field in cmd and not isinstance(cmd[field], dict):
            return GuardResult(False, f"{field}_must_be_dict")
    for field in _NON_NEGATIVE_INT_FIELDS:
        if field not in cmd:
            continue
        value = cmd[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return GuardResult(False, f"{field}_must_be_non_negative_int")
    if "key" in cmd and not isinstance(cmd["key"], str):
        return GuardResult(False, "key_must_be_string")
    return _ALLOW


def _validate_pipeline(stages: list, depth: int) -> GuardResult:
    if depth > MAX_PIPELINE_DEPTH:
        return GuardResult(False, "pipeline_too_deep")
    for stage in stages:
        if not isinstance(stage, dict) or len(stage) != 1:
            return GuardResult(False, "stage_must_be_single_key_dict")
        stage_name, stage_val = next(iter(stage.items()))
        if stage_name in BLOCKED_STAGES:
            return GuardResult(False, f"stage_blocked: {stage_name}")
        if stage_name not in ALLOWED_STAGES:
            return GuardResult(False, f"stage_not_allowed: {stage_name}")

        if stage_name == "$facet" and isinstance(stage_val, dict):
            for sub in stage_val.values():
                if not isinstance(sub, list):
                    return GuardResult(False, "facet_sub_must_be_list")
                res = _validate_pipeline(sub, depth + 1)
                if not res.allowed:
                    return res

        if stage_name in ("$lookup", "$graphLookup") and isinstance(stage_val, dict):
            from_val = stage_val.get("from")
            if isinstance(from_val, str) and "." in from_val:
                return GuardResult(False, "lookup_cross_db_blocked")
            sub_pipe = stage_val.get("pipeline")
            if isinstance(sub_pipe, list):
                res = _validate_pipeline(sub_pipe, depth + 1)
                if not res.allowed:
                    return res

        if stage_name == "$unionWith":
            # Shorthand form is a bare collection name; long form carries its
            # own sub-pipeline, which needs the same walk as $lookup's.
            coll = stage_val if isinstance(stage_val, str) else None
            sub_pipe = None
            if isinstance(stage_val, dict):
                coll = stage_val.get("coll")
                sub_pipe = stage_val.get("pipeline")
            if isinstance(coll, str) and "." in coll:
                return GuardResult(False, "union_cross_db_blocked")
            if isinstance(sub_pipe, list):
                res = _validate_pipeline(sub_pipe, depth + 1)
                if not res.allowed:
                    return res

    return _ALLOW


def _find_blocked_operator(obj: Any, depth: int = 0) -> str | None:
    """Walk entire structure; return the first blocked operator key found."""
    if depth > MAX_PIPELINE_DEPTH * 2:
        return "max_walk_depth_exceeded"
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in BLOCKED_OPERATORS_ANYWHERE:
                return k
            found = _find_blocked_operator(v, depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_blocked_operator(item, depth + 1)
            if found is not None:
                return found
    return None
