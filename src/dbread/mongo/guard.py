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

ALLOWED_STAGES = frozenset({
    "$match", "$project", "$group", "$sort", "$limit", "$skip",
    "$count", "$facet", "$bucket", "$bucketAuto", "$unwind",
    "$addFields", "$set", "$replaceRoot", "$replaceWith",
    "$sortByCount", "$densify", "$fill", "$lookup",
    "$redact", "$sample", "$graphLookup",
})

BLOCKED_STAGES = frozenset({
    "$out", "$merge",
    "$function", "$accumulator",
    "$unionWith",
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

        if name == "find":
            flt = cmd.get("filter", {})
            if not isinstance(flt, dict):
                return GuardResult(False, "find_filter_must_be_dict")

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
