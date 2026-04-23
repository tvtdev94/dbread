"""Unit tests for schema inference helpers."""

from __future__ import annotations

from dbread.mongo.schema import bson_type, flatten_doc, infer_schema

# ---- flatten_doc ---------------------------------------------------------


def test_flatten_flat() -> None:
    got = dict(flatten_doc({"name": "alice", "age": 30}))
    assert got == {"name": "alice", "age": 30}


def test_flatten_nested() -> None:
    got = dict(flatten_doc({"address": {"city": "SG", "zip": "1"}, "age": 30}))
    assert got == {"address.city": "SG", "address.zip": "1", "age": 30}


def test_flatten_preserves_arrays() -> None:
    got = dict(flatten_doc({"tags": ["a", "b"]}))
    assert got == {"tags": ["a", "b"]}


def test_flatten_id_stays_flat() -> None:
    got = dict(flatten_doc({"_id": 1, "name": "a"}))
    assert got == {"_id": 1, "name": "a"}


# ---- bson_type ------------------------------------------------------------


def test_bson_type_primitives() -> None:
    assert bson_type(None) == "null"
    assert bson_type(True) == "bool"
    assert bson_type(1) == "int"
    assert bson_type(1.5) == "double"
    assert bson_type("x") == "string"
    assert bson_type({}) == "object"


def test_bson_type_array_homogeneous() -> None:
    assert bson_type(["a", "b"]) == "array<string>"


def test_bson_type_array_mixed() -> None:
    t = bson_type(["a", 1])
    assert t.startswith("array<")
    assert "int" in t and "string" in t


# ---- infer_schema ---------------------------------------------------------


def test_infer_schema_basic() -> None:
    sample = [
        {"_id": 1, "email": "a@x", "age": 10},
        {"_id": 2, "email": "b@x", "age": 20},
    ]
    fields = infer_schema(sample, sample_size=100)
    by_name = {f["name"]: f for f in fields}
    assert set(by_name) == {"_id", "email", "age"}
    assert by_name["_id"]["pk"] is True
    assert by_name["email"]["pk"] is False
    assert by_name["age"]["types"] == ["int"]
    assert by_name["email"]["frequency"] == 1.0


def test_infer_schema_partial_frequency() -> None:
    sample = [
        {"_id": 1, "name": "a"},
        {"_id": 2},
        {"_id": 3},
        {"_id": 4},
    ]
    fields = infer_schema(sample, sample_size=4)
    by = {f["name"]: f for f in fields}
    assert by["name"]["frequency"] == 0.25


def test_infer_schema_mixed_types() -> None:
    sample = [{"_id": 1, "v": 1}, {"_id": 2, "v": "x"}]
    fields = infer_schema(sample, sample_size=2)
    by = {f["name"]: f for f in fields}
    assert set(by["v"]["types"]) == {"int", "string"}


def test_infer_schema_nested() -> None:
    sample = [{"_id": 1, "address": {"city": "SG"}}]
    fields = infer_schema(sample, sample_size=1)
    names = {f["name"] for f in fields}
    assert "address.city" in names


def test_infer_schema_empty_sample() -> None:
    assert infer_schema([], sample_size=100) == []
