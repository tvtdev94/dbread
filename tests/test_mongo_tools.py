"""Unit tests for MongoToolHandlers — MongoClient fully mocked."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dbread.audit import AuditLogger
from dbread.config import Settings
from dbread.connections import ConnectionManager
from dbread.mongo.client import MongoClientManager
from dbread.mongo.tools import MongoToolHandlers
from dbread.rate_limiter import RateLimiter
from dbread.tools import ToolError, ToolHandlers


def _make_settings(tmp_path: Path, *, max_rows: int = 1000) -> Settings:
    return Settings.model_validate({
        "connections": {
            "m": {
                "url": "mongodb://u:p@h:27017/testdb",
                "dialect": "mongodb",
                "rate_limit_per_min": 600,
                "statement_timeout_s": 30,
                "max_rows": max_rows,
                "mongo": {"sample_size": 50},
            },
            "s": {
                "url": "sqlite:///:memory:",
                "dialect": "sqlite",
                "rate_limit_per_min": 600,
                "statement_timeout_s": 10,
                "max_rows": 100,
            },
        },
        "audit": {"path": str(tmp_path / "audit.jsonl"), "rotate_mb": 1},
    })


def _build(tmp_path: Path, *, max_rows: int = 1000) -> tuple[ToolHandlers, MagicMock]:
    settings = _make_settings(tmp_path, max_rows=max_rows)
    cm = ConnectionManager(settings)
    audit = AuditLogger(settings.audit.path, 1)
    rl = RateLimiter(settings)

    fake_db = MagicMock(name="db")
    fake_client = MagicMock(name="client")
    fake_client.__getitem__.return_value = fake_db

    mongo_mgr = MongoClientManager(settings)
    mongo_mgr._clients["m"] = fake_client

    mongo_handlers = MongoToolHandlers(cm, mongo_mgr, rl, audit)
    from dbread.sql_guard import SqlGuard

    handlers = ToolHandlers(
        settings=settings,
        conn_mgr=cm,
        guard=SqlGuard(),
        rate_limiter=rl,
        audit=audit,
        mongo=mongo_handlers,
    )
    return handlers, fake_db


# ---- list_tables / describe_table -----------------------------------------


def test_list_tables_sorted(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    db.list_collection_names.return_value = ["orders", "users", "audit"]
    assert handlers.list_tables("m") == ["audit", "orders", "users"]


def test_describe_table_samples_and_infers(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    coll = MagicMock()
    coll.aggregate.return_value = iter([
        {"_id": 1, "email": "a@x", "age": 10},
        {"_id": 2, "email": "b@x", "age": 20},
    ])
    coll.list_indexes.return_value = [
        {"name": "_id_", "key": {"_id": 1}, "unique": True},
        {"name": "email_1", "key": {"email": 1}, "unique": True},
    ]
    db.__getitem__.return_value = coll

    res = handlers.describe_table("m", "users")
    assert res["source"] == "sampled"
    assert res["sample_size"] == 2
    fields = {f["name"]: f for f in res["fields"]}
    assert fields["_id"]["pk"] is True
    # $sample stage used with configured sample_size
    args, _ = coll.aggregate.call_args
    assert args[0] == [{"$sample": {"size": 50}}]
    assert len(res["indexes"]) == 2


# ---- query dispatch: cross-mismatch errors --------------------------------


def test_sql_to_mongo_conn_rejected(tmp_path: Path) -> None:
    handlers, _ = _build(tmp_path)
    with pytest.raises(ToolError, match="command required"):
        handlers.query("m", sql="SELECT 1")


def test_command_to_sql_conn_rejected(tmp_path: Path) -> None:
    handlers, _ = _build(tmp_path)
    with pytest.raises(ToolError, match="sql required"):
        handlers.query("s", command={"find": "x"})


def test_mongo_without_command_rejected(tmp_path: Path) -> None:
    handlers, _ = _build(tmp_path)
    with pytest.raises(ToolError, match="command field required"):
        handlers.query("m")


# ---- query execution: find / aggregate / count / distinct ----------------


def test_query_find_returns_rows(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    coll = MagicMock()
    cursor = MagicMock()
    cursor.limit.return_value = cursor
    cursor.max_time_ms.return_value = iter([
        {"_id": 1, "email": "a"},
        {"_id": 2, "email": "b"},
    ])
    coll.find.return_value = cursor
    db.__getitem__.return_value = coll

    res = handlers.query("m", command={"find": "users", "filter": {"status": "active"}})
    assert res["columns"] == ["_id", "email"]
    assert res["row_count"] == 2
    # limit injection happened → cap = max_rows = 1000
    cursor.limit.assert_called_once_with(1000)


def test_query_find_respects_max_rows(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    coll = MagicMock()
    cursor = MagicMock()
    cursor.limit.return_value = cursor
    cursor.max_time_ms.return_value = iter([{"_id": i} for i in range(10)])
    coll.find.return_value = cursor
    db.__getitem__.return_value = coll

    handlers.query("m", command={"find": "u"}, max_rows=5)
    cursor.limit.assert_called_once_with(5)


def test_query_aggregate(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    coll = MagicMock()
    coll.aggregate.return_value = iter([{"_id": "active", "n": 3}])
    db.__getitem__.return_value = coll

    res = handlers.query("m", command={
        "aggregate": "users",
        "pipeline": [{"$group": {"_id": "$status", "n": {"$sum": 1}}}],
    })
    assert res["columns"] == ["_id", "n"]
    assert res["rows"] == [["active", 3]]
    # pipeline had $limit appended
    args, kwargs = coll.aggregate.call_args
    assert args[0][-1] == {"$limit": 1000}
    assert "maxTimeMS" in kwargs


def test_query_count(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    coll = MagicMock()
    coll.count_documents.return_value = 42
    db.__getitem__.return_value = coll

    res = handlers.query("m", command={"countDocuments": "users", "filter": {"status": "active"}})
    assert res["rows"] == [[42]]
    assert res["columns"] == ["count"]


def test_query_distinct_caps_result(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path, max_rows=3)
    coll = MagicMock()
    coll.distinct.return_value = ["a", "b", "c", "d", "e"]
    db.__getitem__.return_value = coll

    res = handlers.query("m", command={"distinct": "users", "key": "status"})
    assert res["columns"] == ["status"]
    assert len(res["rows"]) == 3


# ---- guard rejection path -------------------------------------------------


def test_query_guard_rejects_out_and_audits(tmp_path: Path) -> None:
    handlers, _ = _build(tmp_path)
    with pytest.raises(ToolError, match="mongo_guard"):
        handlers.query("m", command={"aggregate": "c", "pipeline": [{"$out": "leak"}]})
    # audit log should contain a rejected record
    content = Path(handlers.audit.path).read_text(encoding="utf-8")
    assert '"status": "rejected"' in content
    assert "$out" in content


# ---- mongo_not_configured -------------------------------------------------


def test_mongo_not_configured_raises(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    cm = ConnectionManager(settings)
    from dbread.sql_guard import SqlGuard

    handlers = ToolHandlers(
        settings=settings,
        conn_mgr=cm,
        guard=SqlGuard(),
        rate_limiter=RateLimiter(settings),
        audit=AuditLogger(settings.audit.path, 1),
        mongo=None,
    )
    with pytest.raises(ToolError, match="mongo_not_configured"):
        handlers.list_tables("m")


# ---- explain --------------------------------------------------------------


def test_explain_mongo(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    db.command.return_value = {"queryPlanner": {"winningPlan": {}}}
    res = handlers.explain("m", command={"find": "users", "filter": {}})
    assert "plan" in res
    # verbosity passed through
    _, kwargs = db.command.call_args
    assert kwargs.get("verbosity") == "queryPlanner"


def test_query_audit_redacts_when_flag_on(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    cm = ConnectionManager(settings)
    audit = AuditLogger(settings.audit.path, 1, redact_literals=True)
    rl = RateLimiter(settings)

    fake_db = MagicMock()
    coll = MagicMock()
    cursor = MagicMock()
    cursor.limit.return_value = cursor
    cursor.max_time_ms.return_value = iter([{"_id": 1, "email": "a@x"}])
    coll.find.return_value = cursor
    fake_db.__getitem__.return_value = coll
    fake_client = MagicMock()
    fake_client.__getitem__.return_value = fake_db
    mgr = MongoClientManager(settings)
    mgr._clients["m"] = fake_client
    mongo_handlers = MongoToolHandlers(cm, mgr, rl, audit)

    mongo_handlers.query("m", {"find": "users", "filter": {"email": "alice@x.com"}})

    import json as _json
    lines = Path(settings.audit.path).read_text(encoding="utf-8").splitlines()
    rec = _json.loads(lines[-1])
    logged_cmd = _json.loads(rec["sql"])
    assert logged_cmd["filter"]["email"] == "?"
    assert "alice@x.com" not in rec["sql"]
