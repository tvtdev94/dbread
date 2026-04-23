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
        names = handlers.list_tables("m")
        assert {"users", "orders"} <= set(names)
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
