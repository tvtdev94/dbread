"""E2E tests for the MongoDB dialect — exercises the full stack.

Each test spins up (or reaches) a live mongo:7 seeded with `users` + `orders`
and walks through Settings.load → ToolHandlers → MongoToolHandlers →
MongoGuard → pymongo. Requires docker compose or a preseeded external Mongo.
"""

from __future__ import annotations

import pathlib

import pytest

from dbread.tools import ToolError

from .conftest import build_mongo_handlers

pytestmark = pytest.mark.integration


def _handlers(mongo_url: str, tmp_path: pathlib.Path):
    return build_mongo_handlers(mongo_url, tmp_path)


def test_list_tables(mongo_url: str, tmp_path: pathlib.Path) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        names = {row["name"] for row in handlers.list_tables("m")}
        assert {"users", "orders"} <= names
    finally:
        mgr.close_all()


def test_describe_table_users(mongo_url: str, tmp_path: pathlib.Path) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.describe_table("m", "users")
        assert res["source"] == "sampled"
        names = {f["name"] for f in res["fields"]}
        assert "_id" in names and "email" in names
        pk_field = next(f for f in res["fields"] if f["name"] == "_id")
        assert pk_field["pk"] is True
    finally:
        mgr.close_all()


def test_query_find_active(mongo_url: str, tmp_path: pathlib.Path) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.query("m", command={
            "find": "users", "filter": {"status": "active"},
        })
        assert res["row_count"] == 2
    finally:
        mgr.close_all()


def test_query_aggregate_group(mongo_url: str, tmp_path: pathlib.Path) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.query("m", command={
            "aggregate": "orders",
            "pipeline": [{"$group": {"_id": "$status", "total": {"$sum": "$amount"}}}],
        })
        totals = {r[0]: r[1] for r in res["rows"]}
        assert totals.get("paid") == 300
        assert totals.get("refunded") == 50
    finally:
        mgr.close_all()


def test_query_count_documents(mongo_url: str, tmp_path: pathlib.Path) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.query("m", command={"countDocuments": "users", "filter": {}})
        assert res["rows"][0][0] == 3
    finally:
        mgr.close_all()


def test_query_distinct(mongo_url: str, tmp_path: pathlib.Path) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.query("m", command={"distinct": "users", "key": "status"})
        statuses = {r[0] for r in res["rows"]}
        assert statuses == {"active", "inactive"}
    finally:
        mgr.close_all()


def test_explain_mongo(mongo_url: str, tmp_path: pathlib.Path) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.explain("m", command={"find": "users", "filter": {}})
        assert "plan" in res
        assert "queryPlanner" in str(res["plan"])
    finally:
        mgr.close_all()


def test_layer0_server_rejects_insert(mongo_url: str, tmp_path: pathlib.Path) -> None:
    """Bypass guard — confirm DB user genuinely lacks write privileges."""
    from pymongo import MongoClient
    from pymongo.errors import OperationFailure
    client = MongoClient(mongo_url)
    try:
        with pytest.raises(OperationFailure, match="not authorized|unauthorized"):
            client["dbread_test"]["users"].insert_one({"x": 1})
    finally:
        client.close()


def test_layer1_guard_rejects_out_stage(mongo_url: str, tmp_path: pathlib.Path) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        with pytest.raises(ToolError, match="mongo_guard"):
            handlers.query("m", command={
                "aggregate": "users", "pipeline": [{"$out": "leak"}],
            })
    finally:
        mgr.close_all()


def test_limit_injection_caps_result(mongo_url: str, tmp_path: pathlib.Path) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.query("m", command={"find": "users"}, max_rows=2)
        assert res["row_count"] == 2
        assert res["truncated"] is True
    finally:
        mgr.close_all()


# ---- find option fidelity against a real server ---------------------------


def test_find_sort_is_applied(mongo_url: str, tmp_path: pathlib.Path) -> None:
    """Seeded _id values are 1,2,3; a descending sort must invert them."""
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.query("m", command={
            "find": "users", "projection": {"_id": 1}, "sort": {"_id": -1},
        })
        assert [row[0] for row in res["rows"]] == [3, 2, 1]
    finally:
        mgr.close_all()


def test_find_skip_is_applied(mongo_url: str, tmp_path: pathlib.Path) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.query("m", command={
            "find": "users", "projection": {"_id": 1}, "skip": 2, "sort": {"_id": 1},
        })
        assert [row[0] for row in res["rows"]] == [3]
    finally:
        mgr.close_all()


def test_newest_document_query(mongo_url: str, tmp_path: pathlib.Path) -> None:
    """The shape every 'latest N rows' question takes."""
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.query("m", command={
            "find": "users", "projection": {"_id": 1},
            "sort": {"created": -1}, "limit": 1,
        })
        assert res["rows"] == [[3]]  # created 2026-03-01, the most recent
    finally:
        mgr.close_all()


def test_unknown_find_field_is_rejected_not_ignored(
    mongo_url: str, tmp_path: pathlib.Path
) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        with pytest.raises(ToolError, match="field_not_allowed"):
            handlers.query("m", command={"find": "users", "bogusOption": 1})
    finally:
        mgr.close_all()


def test_explain_aggregate(mongo_url: str, tmp_path: pathlib.Path) -> None:
    """Aggregate explain needs a cursor document the caller never supplies."""
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.explain("m", command={
            "aggregate": "orders",
            "pipeline": [{"$group": {"_id": "$status", "n": {"$sum": 1}}}],
        })
        assert "plan" in res
    finally:
        mgr.close_all()


# ---- newly allowlisted read-only stages -----------------------------------


@pytest.mark.parametrize("stage", [
    {"$unset": "amount"},
    {"$setWindowFields": {
        "sortBy": {"_id": 1},
        "output": {"running": {
            "$sum": "$amount",
            "window": {"documents": ["unbounded", "current"]},
        }},
    }},
    {"$unionWith": "users"},
])
def test_read_only_stage_allowed(
    stage: dict, mongo_url: str, tmp_path: pathlib.Path
) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        assert handlers.query("m", command={
            "aggregate": "orders", "pipeline": [stage],
        })["row_count"] > 0
    finally:
        mgr.close_all()


def test_union_with_cross_db_still_blocked(
    mongo_url: str, tmp_path: pathlib.Path
) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        with pytest.raises(ToolError, match="cross_db"):
            handlers.query("m", command={
                "aggregate": "orders",
                "pipeline": [{"$unionWith": {"coll": "otherdb.secrets"}}],
            })
    finally:
        mgr.close_all()


# ---- debug-fast tools ------------------------------------------------------


def test_sample_table(mongo_url: str, tmp_path: pathlib.Path) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.sample_table("m", "users", n=2)
        assert res["ordered_by"] == "_id"
        assert [row[0] for row in res["rows"]] == [3, 2]
    finally:
        mgr.close_all()


def test_profile_table(mongo_url: str, tmp_path: pathlib.Path) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        res = handlers.profile_table("m", "users")
        fields = {f["name"]: f for f in res["fields"]}
        assert fields["status"]["distinct_count"] == 2
        # only the third seeded user carries tags
        assert fields["tags"]["null_count"] == 2
    finally:
        mgr.close_all()


def test_list_schemas_returns_pinned_database(
    mongo_url: str, tmp_path: pathlib.Path
) -> None:
    handlers, mgr = _handlers(mongo_url, tmp_path)
    try:
        assert handlers.list_schemas("m") == ["dbread_test"]
    finally:
        mgr.close_all()
