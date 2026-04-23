"""Adversarial test suite for MongoGuard — attacker-style evasion attempts.

Every case must be REJECTED with a clear reason. New Mongo versions introducing
write stages are covered by the allowlist default-deny posture.
"""

from __future__ import annotations

import pytest

from dbread.mongo.guard import MAX_PIPELINE_DEPTH, MongoGuard


@pytest.fixture
def guard() -> MongoGuard:
    return MongoGuard()


# ---- Write-stage smuggling ------------------------------------------------


def test_top_level_out_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({"aggregate": "c", "pipeline": [{"$out": "leak"}]})
    assert not res.allowed
    assert "$out" in res.reason


def test_trailing_merge_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "aggregate": "c",
        "pipeline": [{"$match": {}}, {"$merge": "target"}],
    })
    assert not res.allowed
    assert "$merge" in res.reason


def test_out_nested_in_facet_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "aggregate": "c",
        "pipeline": [{"$facet": {"leak": [{"$out": "x"}]}}],
    })
    assert not res.allowed
    assert "$out" in res.reason


def test_merge_nested_in_lookup_pipeline_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "aggregate": "orders",
        "pipeline": [{"$lookup": {
            "from": "users",
            "pipeline": [{"$merge": "target"}],
            "as": "u",
        }}],
    })
    assert not res.allowed
    assert "$merge" in res.reason


# ---- JS-exec smuggling ----------------------------------------------------


def test_dollar_function_in_expr_rejected(guard: MongoGuard) -> None:
    fn = {"body": "function(){return 1}", "args": [], "lang": "js"}
    res = guard.validate_command({
        "find": "users",
        "filter": {"$expr": {"$function": fn}},
    })
    assert not res.allowed
    assert "$function" in res.reason


def test_dollar_accumulator_in_group_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "aggregate": "users",
        "pipeline": [{"$group": {
            "_id": "$status",
            "x": {"$accumulator": {
                "init": "function(){return 0}",
                "accumulate": "function(){}",
                "accumulateArgs": [],
                "merge": "function(){}",
                "lang": "js",
            }},
        }}],
    })
    assert not res.allowed
    assert "$accumulator" in res.reason


def test_dollar_where_in_filter_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({"find": "users", "filter": {"$where": "this.x == 1"}})
    assert not res.allowed
    assert "$where" in res.reason


# ---- Command-level blocks -------------------------------------------------


def test_map_reduce_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "mapReduce": "users", "map": "f", "reduce": "g", "out": {"inline": 1},
    })
    assert not res.allowed
    assert "command_not_allowed" in res.reason


def test_insert_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({"insert": "users", "documents": [{"x": 1}]})
    assert not res.allowed
    assert "command_not_allowed" in res.reason


def test_update_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "update": "users",
        "updates": [{"q": {}, "u": {"$set": {"x": 1}}}],
    })
    assert not res.allowed


def test_delete_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({"delete": "users", "deletes": [{"q": {}, "limit": 0}]})
    assert not res.allowed


# ---- Pipeline-shape attacks ----------------------------------------------


def test_deep_nesting_rejected(guard: MongoGuard) -> None:
    # Build $facet -> $facet -> ... -> DEEP nesting
    sub: list = [{"$match": {}}]
    for _ in range(MAX_PIPELINE_DEPTH + 2):
        sub = [{"$facet": {"a": sub}}]
    res = guard.validate_command({"aggregate": "c", "pipeline": sub})
    assert not res.allowed


def test_unknown_stage_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({"aggregate": "c", "pipeline": [{"$invented": {}}]})
    assert not res.allowed
    assert "stage_not_allowed" in res.reason


def test_multi_key_stage_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "aggregate": "c",
        "pipeline": [{"$match": {}, "$limit": 10}],
    })
    assert not res.allowed
    assert "single_key" in res.reason


def test_cross_db_lookup_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "aggregate": "c",
        "pipeline": [{"$lookup": {"from": "otherdb.coll", "as": "x"}}],
    })
    assert not res.allowed
    assert "cross_db" in res.reason


def test_union_with_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "aggregate": "c",
        "pipeline": [{"$unionWith": "other"}],
    })
    assert not res.allowed
    assert "$unionWith" in res.reason


def test_empty_command_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({})
    assert not res.allowed
    assert res.reason == "empty_command"


def test_non_dict_command_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command(["find", "users"])  # type: ignore[arg-type]
    assert not res.allowed


def test_aggregate_missing_pipeline_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({"aggregate": "users"})
    assert not res.allowed


def test_find_filter_not_dict_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({"find": "u", "filter": "bad"})
    assert not res.allowed


def test_out_buried_deep_in_lookup_rejected(guard: MongoGuard) -> None:
    """$out hidden 3 levels deep in nested $lookup.pipeline still blocked."""
    res = guard.validate_command({
        "aggregate": "a",
        "pipeline": [{"$lookup": {
            "from": "b",
            "as": "x",
            "pipeline": [{"$lookup": {
                "from": "c",
                "as": "y",
                "pipeline": [{"$out": "leak"}],
            }}],
        }}],
    })
    assert not res.allowed
    assert "$out" in res.reason


def test_function_in_match_subexpr_rejected(guard: MongoGuard) -> None:
    """$function nested anywhere in a filter dict is caught by walk."""
    res = guard.validate_command({
        "aggregate": "u",
        "pipeline": [{"$match": {"$and": [
            {"a": 1},
            {"$expr": {"$function": {"body": "x", "args": [], "lang": "js"}}},
        ]}}],
    })
    assert not res.allowed
    assert "$function" in res.reason
