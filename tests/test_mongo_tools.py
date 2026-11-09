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


def test_list_tables_reports_kind_and_hides_system_namespaces(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    db.list_collections.return_value = [
        {"name": "users", "type": "collection", "options": {}},
        {"name": "active_users", "type": "view", "options": {"viewOn": "users"}},
        {"name": "system.views", "type": "collection", "options": {}},
        {"name": "metrics", "type": "timeseries", "options": {}},
    ]
    assert handlers.mongo.list_tables("m") == [
        {"name": "active_users", "type": "view"},
        {"name": "metrics", "type": "timeseries"},
        {"name": "users", "type": "collection"},
    ]


def _describable(db: MagicMock, docs: list[dict]) -> MagicMock:
    coll = MagicMock()
    coll.aggregate.return_value = iter(docs)
    coll.list_indexes.return_value = [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "email_1", "key": {"email": 1}, "unique": True},
    ]
    db.__getitem__.return_value = coll
    db.list_collections.return_value = [{
        "name": "users", "type": "collection",
        "options": {"validator": {"$jsonSchema": {"required": ["email"]}}},
    }]
    return coll


def test_describe_table_samples_and_infers(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    # The server hands back each sampled document wrapped as {doc: ...}.
    coll = _describable(db, [
        {"doc": {"_id": 1, "email": "a@x", "age": 10}},
        {"doc": {"_id": 2, "email": "b@x"}},
    ])

    res = handlers.mongo.describe_table("m", "users")
    assert res["source"] == "sampled"
    assert res["sample_size"] == 2
    assert res["type"] == "collection"
    assert res["validator"] == {"$jsonSchema": {"required": ["email"]}}
    fields = {f["name"]: f for f in res["fields"]}
    assert fields["_id"]["pk"] is True
    # A field absent from one document is half-present, not present-but-null.
    assert fields["age"]["frequency"] == 0.5
    assert fields["age"]["types"] == ["int"]
    assert len(res["indexes"]) == 2
    # The sample is drawn through the guarded query path at the configured size.
    pipeline = coll.aggregate.call_args.args[0]
    assert pipeline[0] == {"$sample": {"size": 50}}
    assert pipeline[-1] == {"$limit": 50}
    assert "maxTimeMS" in coll.aggregate.call_args.kwargs


def test_describe_table_sample_is_audited(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    _describable(db, [])
    handlers.mongo.describe_table("m", "users")
    import json as _json
    logged = [
        _json.loads(_json.loads(line)["sql"])
        for line in Path(handlers.audit.path).read_text(encoding="utf-8").splitlines()
    ]
    # One row for the catalog read, one for the sample draw.
    assert [next(iter(cmd)) for cmd in logged] == ["listCollections", "aggregate"]
    assert logged[1]["pipeline"][0] == {"$sample": {"size": 50}}


def test_describe_unknown_collection_is_an_error_not_an_empty_schema(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    db.list_collections.return_value = []
    with pytest.raises(ToolError, match="unknown_table: nope"):
        handlers.mongo.describe_table("m", "nope")


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


# ---- find option fidelity --------------------------------------------------


def _find_cursor(db: MagicMock) -> tuple[MagicMock, MagicMock]:
    """Wire a chainable cursor so each option can be asserted individually."""
    coll = MagicMock()
    db.__getitem__.return_value = coll
    cursor = MagicMock()
    coll.find.return_value = cursor
    for method in ("sort", "skip", "hint", "collation", "batch_size", "comment", "limit"):
        getattr(cursor, method).return_value = cursor
    cursor.max_time_ms.return_value = iter([])
    return coll, cursor


def test_find_applies_sort(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    _, cursor = _find_cursor(db)
    handlers.query("m", command={"find": "u", "sort": {"_id": -1}})
    cursor.sort.assert_called_once_with({"_id": -1})


def test_find_applies_skip(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    _, cursor = _find_cursor(db)
    handlers.query("m", command={"find": "u", "skip": 5})
    cursor.skip.assert_called_once_with(5)


def test_find_applies_hint_and_collation(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    _, cursor = _find_cursor(db)
    handlers.query("m", command={
        "find": "u", "hint": "idx_email", "collation": {"locale": "en"},
    })
    cursor.hint.assert_called_once_with("idx_email")
    cursor.collation.assert_called_once_with({"locale": "en"})


def test_find_without_options_leaves_cursor_alone(tmp_path: Path) -> None:
    handlers, db = _build(tmp_path)
    _, cursor = _find_cursor(db)
    handlers.query("m", command={"find": "u"})
    cursor.sort.assert_not_called()
    cursor.skip.assert_not_called()


def test_distinct_passes_max_time_ms(tmp_path: Path) -> None:
    """statement_timeout_s must reach every command, distinct included."""
    handlers, db = _build(tmp_path)
    coll = MagicMock()
    db.__getitem__.return_value = coll
    coll.distinct.return_value = ["a"]
    handlers.query("m", command={"distinct": "u", "key": "status"})
    assert coll.distinct.call_args.kwargs["maxTimeMS"] == 30_000


def test_explain_aggregate_injects_cursor(tmp_path: Path) -> None:
    """The server rejects an aggregate explain that carries no cursor."""
    handlers, db = _build(tmp_path)
    handlers.explain("m", command={"aggregate": "u", "pipeline": []})
    inner = db.command.call_args.args[1]
    assert inner["cursor"] == {}


def test_explain_maps_count_documents_to_server_command(tmp_path: Path) -> None:
    """countDocuments is a driver helper; the server only knows `count`."""
    handlers, db = _build(tmp_path)
    handlers.explain("m", command={"countDocuments": "u", "filter": {"a": 1}})
    inner = db.command.call_args.args[1]
    assert "count" in inner
    assert inner["query"] == {"a": 1}
