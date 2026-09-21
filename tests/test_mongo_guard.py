"""Happy-path validation + inject_limit tests for MongoGuard."""

from __future__ import annotations

import pytest

from dbread.mongo.guard import MongoGuard


@pytest.fixture
def guard() -> MongoGuard:
    return MongoGuard()


# ---- validate_command: allowed commands ----------------------------------


def test_find_simple(guard: MongoGuard) -> None:
    res = guard.validate_command({"find": "users", "filter": {"status": "active"}})
    assert res.allowed, res.reason


def test_count_documents(guard: MongoGuard) -> None:
    assert guard.validate_command({"countDocuments": "users", "filter": {}}).allowed


def test_count_alias(guard: MongoGuard) -> None:
    assert guard.validate_command({"count": "users"}).allowed


def test_estimated_document_count(guard: MongoGuard) -> None:
    assert guard.validate_command({"estimatedDocumentCount": "users"}).allowed


def test_distinct(guard: MongoGuard) -> None:
    assert guard.validate_command({"distinct": "users", "key": "status"}).allowed


def test_aggregate_simple(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "aggregate": "users",
        "pipeline": [
            {"$match": {"status": "active"}},
            {"$project": {"email": 1}},
            {"$sort": {"_id": 1}},
        ],
    })
    assert res.allowed, res.reason


def test_aggregate_facet_allowed_stages(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "aggregate": "users",
        "pipeline": [{"$facet": {
            "by_status": [{"$group": {"_id": "$status", "n": {"$sum": 1}}}],
            "sample": [{"$sample": {"size": 10}}],
        }}],
    })
    assert res.allowed, res.reason


def test_lookup_same_db(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "aggregate": "orders",
        "pipeline": [{"$lookup": {
            "from": "users",
            "localField": "user_id",
            "foreignField": "_id",
            "as": "user",
        }}],
    })
    assert res.allowed, res.reason


def test_lookup_with_sub_pipeline(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "aggregate": "orders",
        "pipeline": [{"$lookup": {
            "from": "users",
            "pipeline": [{"$match": {"status": "active"}}, {"$project": {"_id": 1}}],
            "as": "user",
        }}],
    })
    assert res.allowed, res.reason


# ---- inject_limit ---------------------------------------------------------


def test_inject_limit_find_no_limit(guard: MongoGuard) -> None:
    out = guard.inject_limit({"find": "u"}, cap=100)
    assert out["limit"] == 100


def test_inject_limit_find_existing_too_large(guard: MongoGuard) -> None:
    out = guard.inject_limit({"find": "u", "limit": 500}, cap=100)
    assert out["limit"] == 100


def test_inject_limit_find_existing_within_cap(guard: MongoGuard) -> None:
    out = guard.inject_limit({"find": "u", "limit": 50}, cap=100)
    assert out["limit"] == 50


def test_inject_limit_aggregate_append(guard: MongoGuard) -> None:
    out = guard.inject_limit({"aggregate": "u", "pipeline": [{"$match": {}}]}, cap=100)
    assert out["pipeline"][-1] == {"$limit": 100}


def test_inject_limit_aggregate_clamps_existing(guard: MongoGuard) -> None:
    out = guard.inject_limit({
        "aggregate": "u",
        "pipeline": [{"$match": {}}, {"$limit": 500}],
    }, cap=100)
    assert out["pipeline"][-1] == {"$limit": 100}
    assert len(out["pipeline"]) == 2


def test_inject_limit_aggregate_keeps_smaller_existing(guard: MongoGuard) -> None:
    out = guard.inject_limit({
        "aggregate": "u",
        "pipeline": [{"$limit": 25}],
    }, cap=100)
    assert out["pipeline"][-1] == {"$limit": 25}


def test_inject_limit_count_is_noop(guard: MongoGuard) -> None:
    out = guard.inject_limit({"countDocuments": "u"}, cap=100)
    assert out == {"countDocuments": "u"}


def test_inject_limit_does_not_mutate_input(guard: MongoGuard) -> None:
    cmd = {"find": "u"}
    guard.inject_limit(cmd, cap=50)
    assert "limit" not in cmd


# ---- guard/executor field contract ----------------------------------------


def test_guard_and_executor_agree_on_fields() -> None:
    """The invariant that keeps silently-dropped options impossible.

    `find` once accepted `sort` and then ignored it at execution, returning
    unsorted rows with no error. Any field the guard lets through must be one
    the executor actually applies.
    """
    from dbread.mongo.guard import COMMAND_FIELDS
    from dbread.mongo.tools import HANDLED_FIELDS

    assert COMMAND_FIELDS == HANDLED_FIELDS


def test_every_allowed_command_declares_fields() -> None:
    from dbread.mongo.guard import ALLOWED_COMMANDS, COMMAND_FIELDS

    assert set(COMMAND_FIELDS) == set(ALLOWED_COMMANDS)


def test_unknown_top_level_field_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({"find": "u", "bogus": 1})
    assert not res.allowed
    assert res.reason == "field_not_allowed: bogus"


def test_find_accepts_sort_skip_hint_collation(guard: MongoGuard) -> None:
    res = guard.validate_command({
        "find": "u", "sort": {"_id": -1}, "skip": 5,
        "hint": "idx", "collation": {"locale": "en"},
    })
    assert res.allowed, res.reason


def test_negative_skip_rejected(guard: MongoGuard) -> None:
    res = guard.validate_command({"find": "u", "skip": -1})
    assert not res.allowed
    assert "non_negative" in res.reason


def test_sort_must_be_dict(guard: MongoGuard) -> None:
    res = guard.validate_command({"find": "u", "sort": "_id"})
    assert not res.allowed
    assert res.reason == "sort_must_be_dict"


def test_aggregate_rejects_find_only_field(guard: MongoGuard) -> None:
    """Field sets are per command, not one shared bag."""
    res = guard.validate_command({
        "aggregate": "u", "pipeline": [], "projection": {"a": 1},
    })
    assert not res.allowed
