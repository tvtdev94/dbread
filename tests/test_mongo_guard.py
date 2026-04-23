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
