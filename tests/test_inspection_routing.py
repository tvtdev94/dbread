"""Which backend a tool call reaches, and what `database` does on each.

The MongoDB side is a recording stand-in: these tests own the routing in
`ToolHandlers`, not MongoDB behaviour (tests/test_mongo_*.py own that).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dbread.audit import AuditLogger
from dbread.config import Settings
from dbread.connections import ConnectionManager
from dbread.rate_limiter import RateLimiter
from dbread.sql_guard import SqlGuard
from dbread.tools import ToolError, ToolHandlers


class RecordingMongo:
    """Records each MongoToolHandlers call and answers with a marker."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str) -> Callable[..., Any]:
        def record(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, args, kwargs))
            return {"from": name}
        return record


def _handlers(tmp_path: Path, mongo: RecordingMongo | None) -> ToolHandlers:
    settings = Settings.model_validate({
        "connections": {
            "m": {"url": "mongodb://u:p@h:27017/app", "dialect": "mongodb"},
            "s": {"url": f"sqlite:///{tmp_path / 's.db'}", "dialect": "sqlite"},
        },
        "audit": {"path": str(tmp_path / "audit.jsonl"), "rotate_mb": 1},
    })
    return ToolHandlers(
        settings=settings,
        conn_mgr=ConnectionManager(settings),
        guard=SqlGuard(),
        rate_limiter=RateLimiter(settings),
        audit=AuditLogger(settings.audit.path, 1),
        mongo=mongo,
    )


# (tool call on connection "m" with database="crm", expected mongo method)
DATABASE_CALLS: list[tuple[str, Callable[[ToolHandlers], Any], str]] = [
    ("list_tables", lambda h: h.list_tables("m", database="crm"), "list_tables"),
    ("list_schemas", lambda h: h.list_schemas("m", database="crm"), "list_schemas"),
    ("describe_table", lambda h: h.describe_table("m", "c", database="crm"), "describe_table"),
    ("sample_table", lambda h: h.sample_table("m", "c", database="crm"), "query"),
    ("profile_table", lambda h: h.profile_table("m", "c", database="crm"), "profile_table"),
    ("query", lambda h: h.query("m", command={"find": "c"}, database="crm"), "query"),
    ("explain", lambda h: h.explain("m", command={"find": "c"}, database="crm"), "explain"),
    ("collection_stats", lambda h: h.collection_stats("m", "c", database="crm"),
     "collection_stats"),
    ("database_overview", lambda h: h.database_overview("m", database="crm"),
     "database_overview"),
    ("search_schema", lambda h: h.search_schema("m", "mail", database="crm"), "search_schema"),
    ("check_integrity", lambda h: h.check_integrity("m", "c", database="crm"),
     "check_integrity"),
]


@pytest.mark.parametrize(
    ("call", "method"),
    [(call, method) for _, call, method in DATABASE_CALLS],
    ids=[tool for tool, _, _ in DATABASE_CALLS],
)
def test_mongo_tools_forward_database(tmp_path: Path, call: Callable, method: str) -> None:
    mongo = RecordingMongo()
    call(_handlers(tmp_path, mongo))
    (name, _, kwargs), = mongo.calls
    assert name == method
    assert kwargs["database"] == "crm"


# The same tools aimed at the SQLite connection, still passing database.
SQL_CALLS: dict[str, Callable[[ToolHandlers], Any]] = {
    "list_tables": lambda h: h.list_tables("s", database="crm"),
    "list_schemas": lambda h: h.list_schemas("s", database="crm"),
    "describe_table": lambda h: h.describe_table("s", "t", database="crm"),
    "sample_table": lambda h: h.sample_table("s", "t", database="crm"),
    "profile_table": lambda h: h.profile_table("s", "t", database="crm"),
    "query": lambda h: h.query("s", sql="SELECT 1", database="crm"),
    "explain": lambda h: h.explain("s", sql="SELECT 1", database="crm"),
    "collection_stats": lambda h: h.collection_stats("s", "t", database="crm"),
    "database_overview": lambda h: h.database_overview("s", database="crm"),
    "search_schema": lambda h: h.search_schema("s", "x", database="crm"),
    "check_integrity": lambda h: h.check_integrity("s", "t", database="crm"),
}


@pytest.mark.parametrize("call", list(SQL_CALLS.values()), ids=list(SQL_CALLS))
def test_sql_connection_rejects_database(tmp_path: Path, call: Callable) -> None:
    mongo = RecordingMongo()
    with pytest.raises(ToolError, match="invalid_input"):
        call(_handlers(tmp_path, mongo))
    assert mongo.calls == []


def test_sql_rejection_names_the_reason(tmp_path: Path) -> None:
    h = _handlers(tmp_path, RecordingMongo())
    with pytest.raises(ToolError, match="database is for mongodb connections only"):
        h.list_tables("s", database="crm")


def test_unset_database_keeps_the_mongo_default(tmp_path: Path) -> None:
    """No database argument reaches the handler, so it uses the URI database."""
    mongo = RecordingMongo()
    _handlers(tmp_path, mongo).describe_table("m", "c")
    assert mongo.calls == [("describe_table", ("m", "c"), {})]


def test_sample_table_sends_newest_first_find(tmp_path: Path) -> None:
    mongo = RecordingMongo()
    out = _handlers(tmp_path, mongo).sample_table("m", "events", n=5, database="crm")
    assert mongo.calls == [(
        "query",
        ("m", {"find": "events", "sort": {"_id": -1}, "limit": 5}, 5),
        {"database": "crm"},
    )]
    assert out["ordered_by"] == "_id"


def test_check_integrity_passes_mongo_arguments(tmp_path: Path) -> None:
    mongo = RecordingMongo()
    refs = [{"field": "user_id", "to": "users"}]
    _handlers(tmp_path, mongo).check_integrity(
        "m", "orders", checks=["orphans"], references=refs, unique_columns=["code"]
    )
    (_, args, kwargs), = mongo.calls
    assert args == ("m", "orders")
    assert kwargs == {"checks": ["orphans"], "references": refs,
                      "unique_columns": ["code"], "database": None}


@pytest.mark.parametrize("tool", ["list_databases", "collection_stats"])
def test_mongo_only_tools_reject_sql(tmp_path: Path, tool: str) -> None:
    h = _handlers(tmp_path, RecordingMongo())
    args = ("s",) if tool == "list_databases" else ("s", "t")
    with pytest.raises(ToolError, match=f"invalid_input: {tool} is for mongodb"):
        getattr(h, tool)(*args)


def test_list_databases_reaches_mongo(tmp_path: Path) -> None:
    mongo = RecordingMongo()
    assert _handlers(tmp_path, mongo).list_databases("m") == {"from": "list_databases"}


def test_db_health_is_unsupported_on_mongo(tmp_path: Path) -> None:
    h = _handlers(tmp_path, RecordingMongo())
    with pytest.raises(ToolError, match="unsupported_dialect: mongodb"):
        h.db_health("m")


def test_diff_schema_snapshots_mongo_side_per_database(tmp_path: Path) -> None:
    mongo = RecordingMongo()
    snaps = {"app": {"tables": {"users": {"type": "collection", "columns": {}, "indexes": {}}}},
             "crm": {"tables": {}}}
    mongo.schema_snapshot = lambda conn, database=None: snaps[database or "app"]
    out = _handlers(tmp_path, mongo).diff_schema("m", "m", database_b="crm")
    assert out["a"] == "m" and out["b"] == "m/crm"
    assert out["tables_only_in_a"] == ["users"]


def test_new_tools_need_mongo_configured(tmp_path: Path) -> None:
    h = _handlers(tmp_path, None)
    with pytest.raises(ToolError, match="mongo_not_configured"):
        h.database_overview("m")
