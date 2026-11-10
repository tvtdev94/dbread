"""Unit tests for Mongo `check_integrity`: input validation, per-check error
isolation, and turning server replies into the INTEGRITY shape.

Every check runs through `query`, so a pipeline the guard refused would come
back as that check's `error`; asserting `error is None` therefore also proves
the built pipelines pass the real guard. Server-side correctness of the
pipelines is covered by tests/integration/test_e2e_mongo_inspection.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbread.tools import ToolError

from .test_mongo_inspection import build, coll, sampled


def _stage(pipeline: list) -> str:
    return next(iter(pipeline[0]))


def _reply(coll_name: str, pipeline: list) -> list[dict]:
    if any("$lookup" in s for s in pipeline):
        return [{"total": [{"n": 2}], "examples": [{"_id": 7, "user_id": 99}]}]
    if any("$facet" in s for s in pipeline):  # duplicates
        return [{"total": [{"n": 1}], "examples": [{"_id": {"k0": "a@x"}, "n": 3}]}]
    if _stage(pipeline) == "$sample":
        return sampled({"_id": 1, "email": "a@x", "legacy": None, "plan": "free"})
    # The null/constant `$group`: legacy never set, plan always "free".
    return [{"total": 4, "n0": 4, "lo0": "a@x", "hi0": "z@x", "n1": 0,
             "n2": 4, "lo2": "free", "hi2": "free"}]


@pytest.fixture
def handlers(tmp_path: Path):
    h, client = build(tmp_path)
    db = client["testdb"]
    db.collections = [coll("orders"), coll("users")]
    db.responder = _reply
    return h


def test_all_checks_report_violations_and_examples(handlers) -> None:
    res = handlers.check_integrity(
        "m", "orders",
        references=[{"field": "user_id", "to": "users"}],
        unique_columns=["email"],
    )
    assert res["table"] == "orders"
    assert res["checks"] == [
        {"check": "orphans", "target": "user_id -> users._id", "violations": 2,
         "examples": [{"_id": 7, "user_id": 99}], "error": None},
        {"check": "duplicates", "target": "email", "violations": 1,
         "examples": [{"email": "a@x", "duplicate_count": 3}], "error": None},
        {"check": "null_columns", "target": "orders", "violations": 1,
         "examples": [{"column": "legacy"}], "error": None},
        {"check": "constant_columns", "target": "orders", "violations": 1,
         "examples": [{"column": "plan", "value": "free"}], "error": None},
    ]


def test_defaulted_checks_skip_what_has_no_inputs(handlers) -> None:
    res = handlers.check_integrity("m", "orders")
    assert [c["check"] for c in res["checks"]] == ["null_columns", "constant_columns"]


def test_explicit_duplicates_without_unique_columns_reports_why(handlers) -> None:
    res = handlers.check_integrity("m", "orders", checks=["duplicates"])
    assert res["checks"] == [{"check": "duplicates", "target": "", "violations": None,
                              "examples": [], "error": "unique_columns required"}]


def test_a_number_and_a_boolean_are_not_one_constant(tmp_path: Path) -> None:
    """BSON orders numbers before booleans, so $min/$max can be 1 and true."""
    h, client = build(tmp_path)
    db = client["testdb"]
    db.collections = [coll("orders")]
    db.responder = lambda c, p: (
        sampled({"_id": 1, "flag": 1, "on": True}) if _stage(p) == "$sample"
        else [{"total": 2, "n0": 2, "lo0": 1, "hi0": True, "n1": 2, "lo1": True, "hi1": True}]
    )
    (constants,) = h.check_integrity("m", "orders", checks=["constant_columns"])["checks"]
    assert constants["examples"] == [{"column": "on", "value": True}]


def test_nested_number_and_boolean_are_not_one_constant(tmp_path: Path) -> None:
    """The same BSON split holds inside arrays: [1] and [true] are two values."""
    h, client = build(tmp_path)
    db = client["testdb"]
    db.collections = [coll("orders")]
    db.responder = lambda c, p: (
        sampled({"_id": 1, "tags": [1]}) if _stage(p) == "$sample"
        else [{"total": 2, "n0": 2, "lo0": [1], "hi0": [True]}]
    )
    (constants,) = h.check_integrity("m", "orders", checks=["constant_columns"])["checks"]
    assert constants["violations"] == 0


def test_uuid_constant_is_compared_with_the_connection_codecs(tmp_path: Path) -> None:
    """A URI `uuidRepresentation` decodes UUIDs to uuid.UUID, which the
    default BSON encoder refuses; the comparison must reuse those codecs."""
    import uuid

    from bson.binary import UuidRepresentation
    from bson.codec_options import CodecOptions

    h, client = build(tmp_path)
    db = client["testdb"]
    db.codec_options = CodecOptions(uuid_representation=UuidRepresentation.STANDARD)
    db.collections = [coll("orders")]
    same = uuid.uuid4()
    db.responder = lambda c, p: (
        sampled({"_id": 1, "ref": same}) if _stage(p) == "$sample"
        else [{"total": 2, "n0": 2, "lo0": same, "hi0": same}]
    )
    (constants,) = h.check_integrity("m", "orders", checks=["constant_columns"])["checks"]
    assert constants["error"] is None
    assert constants["examples"] == [{"column": "ref", "value": same}]


def test_one_failing_check_does_not_abort_the_others(tmp_path: Path) -> None:
    h, client = build(tmp_path)
    db = client["testdb"]
    db.collections = [coll("orders"), coll("users")]

    def reply(coll_name: str, pipeline: list) -> list[dict]:
        if any("$lookup" in s for s in pipeline):
            raise RuntimeError("operation exceeded time limit")
        return _reply(coll_name, pipeline)
    db.responder = reply

    res = h.check_integrity(
        "m", "orders", checks=["orphans", "null_columns"],
        references=[{"field": "user_id", "to": "users"}, {"field": "x", "to": "ghosts"}],
    )
    orphans, ghosts, nulls = res["checks"]
    assert "time limit" in orphans["error"]
    # A check that could not run reports no count rather than a clean 0.
    assert (orphans["violations"], orphans["examples"]) == (None, [])
    assert (ghosts["violations"], ghosts["error"]) == (None, "unknown_table: ghosts")
    assert nulls["error"] is None and nulls["violations"] == 1


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"references": [{"field": "a", "to": "other.users"}]}, "same database"),
    ({"references": [{"field": "$a", "to": "users"}]}, "field path"),
    ({"references": "users"}, "must be a list"),
    ({"unique_columns": ["a..b"]}, "field path"),
    ({"checks": ["orphan"]}, "unknown check: orphan"),
    ({"checks": []}, "checks must not be empty"),
])
def test_malformed_input_is_rejected_before_any_read(handlers, kwargs, message) -> None:
    with pytest.raises(ToolError, match=message):
        handlers.check_integrity("m", "orders", **kwargs)
    assert not Path(handlers.audit.path).exists()


def test_unknown_table_is_an_error(handlers) -> None:
    with pytest.raises(ToolError, match="unknown_table: nope"):
        handlers.check_integrity("m", "nope")
