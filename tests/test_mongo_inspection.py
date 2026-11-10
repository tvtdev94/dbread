"""Unit tests for the Mongo inspection tools: database scoping, metadata reads,
list/describe/stats, overview, schema search and snapshot.

The fake client returns canned server replies. What is asserted is dbread's
own work on top of them: allowlist decisions, rate-limit/audit accounting,
that every pipeline it builds passes the real guard, and the contract shapes.
Whether the pipelines compute the right answer on a server is covered by
tests/integration/test_e2e_mongo_inspection.py.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dbread.audit import AuditLogger
from dbread.config import Settings
from dbread.connections import ConnectionManager
from dbread.mongo.client import MongoClientManager
from dbread.mongo.tools import MongoToolHandlers
from dbread.rate_limiter import RateLimiter
from dbread.tools import ToolError

Responder = Callable[[str, list], list[dict]]


class FakeColl:
    def __init__(self, db: FakeDb, name: str) -> None:
        self.db, self.name = db, name

    def aggregate(self, pipeline: list, **kwargs: Any):
        self.db.calls.append((self.name, pipeline))
        return iter(self.db.responder(self.name, pipeline))

    def list_indexes(self):
        return iter(self.db.indexes.get(self.name, []))


class FakeDb:
    def __init__(self, name: str, collections: list[dict] | None = None) -> None:
        self.name = name
        self.collections = collections or []
        self.indexes: dict[str, list[dict]] = {}
        self.responder: Responder = lambda coll, pipeline: []
        self.calls: list[tuple[str, list]] = []

    def list_collections(self, filter: dict | None = None, **_: Any):  # noqa: A002 - pymongo's
        return iter(c for c in self.collections if not filter or c["name"] == filter["name"])

    def __getitem__(self, coll: str) -> FakeColl:
        return FakeColl(self, coll)


class FakeClient:
    def __init__(self, *names: str) -> None:
        self.dbs = {n: FakeDb(n) for n in names}
        self.listed: list[dict] = []
        self.list_calls: list[dict] = []

    def __getitem__(self, name: str) -> FakeDb:
        return self.dbs.setdefault(name, FakeDb(name))

    def list_database_names(self) -> list[str]:
        return list(self.dbs)

    def list_databases(self, nameOnly: bool = False, **kwargs: Any):  # noqa: N803 - pymongo's name
        self.list_calls.append(kwargs)
        if nameOnly:
            return iter({"name": n} for n in self.dbs)
        return iter(self.listed)


def build(
    tmp_path: Path, *, databases: list[str] | None = None, rate: int = 600,
    names: tuple[str, ...] = ("testdb",),
) -> tuple[MongoToolHandlers, FakeClient]:
    settings = Settings.model_validate({
        "connections": {"m": {
            "url": "mongodb://u:p@h:27017/testdb", "dialect": "mongodb",
            "rate_limit_per_min": rate, "max_rows": 1000,
            "mongo": {"sample_size": 20, "databases": databases},
        }},
        "audit": {"path": str(tmp_path / "audit.jsonl"), "rotate_mb": 1},
    })
    client = FakeClient(*names)
    mgr = MongoClientManager(settings)
    mgr._clients["m"] = client  # type: ignore[assignment] - fake stands in for MongoClient
    handlers = MongoToolHandlers(
        ConnectionManager(settings), mgr, RateLimiter(settings),
        AuditLogger(settings.audit.path, 1),
    )
    return handlers, client


def audit_rows(handlers: MongoToolHandlers) -> list[dict]:
    lines = Path(handlers.audit.path).read_text(encoding="utf-8").splitlines()
    return [{**r, "cmd": json.loads(r["sql"])} for r in map(json.loads, lines)]


def coll(name: str, kind: str = "collection", **options: Any) -> dict:
    return {"name": name, "type": kind, "options": options}


def sampled(*docs: dict) -> list[dict]:
    """A `$sample` + `$replaceWith {doc: $$ROOT}` reply."""
    return [{"doc": d} for d in docs]


# ---- database scoping -------------------------------------------------------


def test_database_defaults_to_uri_database(tmp_path: Path) -> None:
    handlers, _ = build(tmp_path)
    assert handlers.db("m").name == "testdb"
    # Naming the URI database needs no allowlist entry.
    assert handlers.db("m", "testdb").name == "testdb"


def test_allowlisted_database_is_reachable_and_audited(tmp_path: Path) -> None:
    handlers, client = build(tmp_path, databases=["crm"])
    client["crm"].responder = lambda c, p: [{"n": 1}]
    handlers.query("m", {"aggregate": "leads", "pipeline": []}, database="crm")
    assert client["crm"].calls and not client["testdb"].calls
    assert audit_rows(handlers)[-1]["cmd"]["$db"] == "crm"


def test_database_outside_allowlist_is_refused_and_audited(tmp_path: Path) -> None:
    handlers, client = build(tmp_path, databases=["crm"], names=("testdb", "hr"))
    with pytest.raises(ToolError, match="database_not_allowed: hr"):
        handlers.query("m", {"find": "salaries"}, database="hr")
    assert not client["hr"].calls
    (row,) = audit_rows(handlers)
    assert row["status"] == "rejected" and row["cmd"]["$db"] == "hr"


def test_star_allows_listed_databases_but_never_system_ones(tmp_path: Path) -> None:
    handlers, _ = build(tmp_path, databases=["*"], names=("testdb", "crm", "admin", "local"))
    assert handlers.db("m", "crm").name == "crm"
    with pytest.raises(ToolError, match="database_not_allowed: admin"):
        handlers.db("m", "admin")
    with pytest.raises(ToolError, match="database_not_allowed: ghost"):
        handlers.db("m", "ghost")
    # The wildcard is configuration syntax, not a database the caller can name.
    with pytest.raises(ToolError, match=r"database_not_allowed: \*"):
        handlers.db("m", "*")
    assert handlers.list_schemas("m") == ["crm", "testdb"]


def test_star_listing_is_time_bounded_and_reused_across_reads(tmp_path: Path) -> None:
    handlers, client = build(tmp_path, databases=["*"], names=("testdb", "crm"))
    for _ in range(3):
        handlers.query("m", {"aggregate": "leads", "pipeline": []}, database="crm")
    # One listing serves every read in the window, and it carries the timeout.
    assert client.list_calls == [{"maxTimeMS": 30_000}]


def test_star_listing_failure_is_a_clean_db_error(tmp_path: Path) -> None:
    handlers, client = build(tmp_path, databases=["*"])

    def denied(**_: Any):
        raise RuntimeError("not authorized to execute listDatabases")
    client.list_databases = denied  # type: ignore[method-assign]
    client.list_database_names = denied  # type: ignore[method-assign]
    with pytest.raises(ToolError, match="db_error: not authorized"):
        handlers.query("m", {"find": "leads"}, database="crm")


def test_list_schemas_without_allowlist_is_the_uri_database(tmp_path: Path) -> None:
    handlers, _ = build(tmp_path)
    assert handlers.list_schemas("m") == ["testdb"]


# ---- metadata reads: one token, one audit row --------------------------------


def test_metadata_read_costs_one_token_and_one_audit_row(tmp_path: Path) -> None:
    handlers, client = build(tmp_path, rate=1)
    client["testdb"].collections = [coll("users")]
    assert handlers.list_tables("m") == [{"name": "users", "type": "collection"}]
    with pytest.raises(ToolError, match="rate_limit_exceeded"):
        handlers.list_tables("m")
    assert [r["status"] for r in audit_rows(handlers)] == ["ok", "rejected"]


def test_metadata_failure_is_audited_and_reported_as_db_error(tmp_path: Path) -> None:
    handlers, client = build(tmp_path)

    def denied(**_: Any):
        raise RuntimeError("not authorized to execute listDatabases")
    client.list_databases = denied  # type: ignore[method-assign]
    with pytest.raises(ToolError, match="db_error: not authorized"):
        handlers.list_databases("m")
    assert [r["status"] for r in audit_rows(handlers)] == ["failed"]


def test_profile_table_tells_missing_from_null_and_reports_type_mix(tmp_path: Path) -> None:
    handlers, client = build(tmp_path)
    client["testdb"].responder = lambda c, p: sampled({"v": 1}, {"v": "1"}, {"w": 2})
    res = handlers.profile_table("m", "t", columns=["v"])
    (v,) = res["fields"]
    assert (v["null_count"], v["types"], v["type_drift"]) == (
        1, {"int": 50.0, "string": 50.0}, True,
    )
    with pytest.raises(ToolError, match="invalid_input: field not present"):
        handlers.profile_table("m", "t", columns=["typo"])


def test_list_databases_reports_only_allowed_with_sizes(tmp_path: Path) -> None:
    handlers, client = build(tmp_path, databases=["crm", "missing"])
    client.listed = [
        {"name": "testdb", "sizeOnDisk": 4096, "empty": False},
        {"name": "crm", "sizeOnDisk": 0, "empty": True},
        {"name": "hr", "sizeOnDisk": 8192, "empty": False},
    ]
    assert handlers.list_databases("m") == [
        {"name": "crm", "size_bytes": 0, "empty": True},
        # Allowlisted but not reported by the server: unknown, not zero.
        {"name": "missing", "size_bytes": None, "empty": None},
        {"name": "testdb", "size_bytes": 4096, "empty": False},
    ]


# ---- collection_stats -------------------------------------------------------


def _stats_responder(index_stats: Callable[[], list[dict]]) -> Responder:
    def reply(coll_name: str, pipeline: list) -> list[dict]:
        stage = next(iter(pipeline[0]))
        if stage == "$collStats":  # one row per shard
            return [
                {"count": 3, "size": 300, "storage": 4096,
                 "index_sizes": {"_id_": 100, "email_1": 50}},
                {"count": 1, "size": 100, "storage": 4096, "index_sizes": {"_id_": 20}},
            ]
        return index_stats()
    return reply


def test_collection_stats_folds_shards_and_index_usage(tmp_path: Path) -> None:
    handlers, client = build(tmp_path)
    client["testdb"].responder = _stats_responder(lambda: [
        {"name": "_id_", "ops": 5, "since": 2}, {"name": "_id_", "ops": 1, "since": 1},
    ])
    res = handlers.collection_stats("m", "users")
    assert {k: res[k] for k in ("count", "size_bytes", "storage_bytes", "avg_doc_bytes")} == {
        "count": 4, "size_bytes": 400, "storage_bytes": 8192, "avg_doc_bytes": 100,
    }
    assert res["indexes"] == [
        {"name": "_id_", "size_bytes": 120, "ops": 6, "since": 1},
        {"name": "email_1", "size_bytes": 50, "ops": None, "since": None},
    ]
    assert "index_usage_error" not in res


def test_collection_stats_survives_missing_index_stats_privilege(tmp_path: Path) -> None:
    def denied() -> list[dict]:
        raise RuntimeError("not authorized on testdb to execute command $indexStats")

    handlers, client = build(tmp_path)
    client["testdb"].responder = _stats_responder(denied)
    res = handlers.collection_stats("m", "users")
    assert "not authorized" in res["index_usage_error"]
    assert [(i["size_bytes"], i["ops"]) for i in res["indexes"]] == [(120, None), (50, None)]


# ---- database_overview / search_schema / schema_snapshot --------------------


def test_database_overview_shape(tmp_path: Path) -> None:
    handlers, client = build(tmp_path)
    db = client["testdb"]
    db.collections = [coll("users"), coll("active_users", "view", viewOn="users")]

    def reply(coll_name: str, pipeline: list) -> list[dict]:
        if "$collStats" in pipeline[0]:
            return [{"count": 2, "size": 80, "storage": 4096, "index_sizes": {"_id_": 512}}]
        return sampled({"_id": 1, "email": "a", "age": 3}, {"_id": 2, "email": "b"})
    db.responder = reply

    res = handlers.database_overview("m")
    assert res["connection"] == "m" and res["schema"] == "testdb"
    assert res["relationships"] == [] and res["source"] == "sampled"
    assert res["truncated"] is False
    by_name = {t["name"]: t for t in res["tables"]}
    assert by_name["users"] == {
        "name": "users", "type": "collection", "row_estimate": 2,
        "size_bytes": 4096 + 512, "columns": 3, "primary_key": ["_id"],
    }
    # A view has no storage of its own, so it is only sampled.
    assert by_name["active_users"]["row_estimate"] is None
    assert by_name["active_users"]["primary_key"] == []


def test_database_overview_reads_dotted_names_and_isolates_a_failing_collection(
    tmp_path: Path,
) -> None:
    handlers, client = build(tmp_path)
    db = client["testdb"]
    db.collections = [coll("broken"), coll("fs.files")]

    def reply(coll_name: str, pipeline: list) -> list[dict]:
        if coll_name == "broken":
            raise RuntimeError("operation exceeded time limit")
        if "$collStats" in pipeline[0]:
            return [{"count": 1, "size": 10, "storage": 100, "index_sizes": {}}]
        return sampled({"_id": 1, "length": 5})
    db.responder = reply

    broken, files = handlers.database_overview("m")["tables"]
    assert files == {"name": "fs.files", "type": "collection", "row_estimate": 1,
                     "size_bytes": 100, "columns": 2, "primary_key": ["_id"]}
    assert broken["error"].startswith("db_error: operation exceeded time limit")
    assert (broken["row_estimate"], broken["size_bytes"], broken["columns"]) == (
        None, None, None,
    )


def test_database_overview_stops_once_the_rate_limit_refuses(tmp_path: Path) -> None:
    # 3 tokens: the catalog read, then one collection's sample + stats.
    handlers, client = build(tmp_path, rate=3)
    db = client["testdb"]
    db.collections = [coll("a"), coll("b"), coll("c")]
    db.responder = lambda c, p: (
        [{"count": 1, "size": 1, "storage": 1, "index_sizes": {}}]
        if "$collStats" in p[0] else sampled({"_id": 1})
    )
    res = handlers.database_overview("m")
    assert [t["name"] for t in res["tables"]] == ["a"]
    assert res["truncated"] is True
    # Exactly one refused read, not one per remaining collection.
    refused = [r for r in audit_rows(handlers) if r["status"] == "rejected"]
    assert len(refused) == 1


@pytest.mark.parametrize("tool", ["database_overview", "schema_snapshot"])
def test_whole_database_reads_stop_at_the_collection_cap(tmp_path: Path, tool: str) -> None:
    from dbread.mongo.catalog import MAX_COLLECTIONS

    handlers, client = build(tmp_path)
    db = client["testdb"]
    db.collections = [coll(f"c{i:03}") for i in range(MAX_COLLECTIONS + 1)]
    res = getattr(handlers, tool)("m")
    tables = res["tables"]
    names = [t["name"] for t in tables] if isinstance(tables, list) else list(tables)
    assert names == [f"c{i:03}" for i in range(MAX_COLLECTIONS)]
    assert res["truncated"] is True
    assert {name for name, _ in db.calls} == {f"c{i:03}" for i in range(MAX_COLLECTIONS)}


def test_search_schema_matches_tables_and_fields_case_insensitively(tmp_path: Path) -> None:
    handlers, client = build(tmp_path)
    db = client["testdb"]
    db.collections = [coll("Users"), coll("orders")]
    db.responder = lambda c, p: (
        sampled({"_id": 1, "user_id": 1}) if c == "orders" else sampled({"_id": 1, "email": "x"})
    )
    res = handlers.search_schema("m", "USER")
    assert res == {"pattern": "USER", "truncated": False, "matches": [
        {"table": "Users", "column": None, "type": None, "kind": "table"},
        {"table": "orders", "column": "user_id", "type": "int", "kind": "column"},
    ]}


def test_search_schema_flags_unsampled_collections_as_truncated(tmp_path: Path) -> None:
    from dbread.mongo.catalog import SEARCH_MAX_COLLECTIONS

    handlers, client = build(tmp_path)
    client["testdb"].collections = [coll(f"c{i:03}") for i in range(SEARCH_MAX_COLLECTIONS + 1)]
    res = handlers.search_schema("m", "zzz")
    assert res["truncated"] is True
    assert len(client["testdb"].calls) == SEARCH_MAX_COLLECTIONS


def test_search_schema_rejects_blank_pattern(tmp_path: Path) -> None:
    handlers, _ = build(tmp_path)
    with pytest.raises(ToolError, match="invalid_input"):
        handlers.search_schema("m", "  ")


def test_schema_snapshot_shape(tmp_path: Path) -> None:
    handlers, client = build(tmp_path)
    db = client["testdb"]
    db.collections = [coll("users"), coll("v", "view")]
    db.indexes = {"users": [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "email_1", "key": {"email": 1, "org": -1}, "unique": True},
    ]}
    db.responder = lambda c, p: sampled({"_id": 1, "email": "a"}, {"_id": 2})
    assert handlers.schema_snapshot("m") == {"tables": {
        "users": {
            "type": "collection",
            "columns": {"_id": {"type": "int", "nullable": False},
                        "email": {"type": "string", "nullable": True}},
            "indexes": {"_id_": {"columns": ["_id"], "unique": False},
                        "email_1": {"columns": ["email", "org"], "unique": True}},
        },
        "v": {
            "type": "view",
            "columns": {"_id": {"type": "int", "nullable": False},
                        "email": {"type": "string", "nullable": True}},
            "indexes": {},
        },
    }, "truncated": False}
