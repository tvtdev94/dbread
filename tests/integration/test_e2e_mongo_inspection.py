"""E2E tests for the Mongo inspection tools against a live, seeded mongo:7.

Seed data: tests/integration/fixtures/mongo-init.js (docker compose) and the
"Seed MongoDB" step in .github/workflows/ci.yml (CI). `ai_ro` holds only the
`read` role on `dbread_test` and `dbread_extra`.

These call MongoToolHandlers directly so the connection can carry a
`mongo:` block (database allowlist, read preference) of its own.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest

from dbread.audit import AuditLogger
from dbread.config import Settings
from dbread.connections import ConnectionManager
from dbread.rate_limiter import RateLimiter
from dbread.tools import ToolError

pytestmark = pytest.mark.integration


@pytest.fixture
def make(mongo_url: str, tmp_path: pathlib.Path) -> Iterator[Any]:
    """Factory for handlers whose connection has the given `mongo:` block."""
    from dbread.mongo.client import MongoClientManager
    from dbread.mongo.tools import MongoToolHandlers

    managers: list[MongoClientManager] = []

    def build(**mongo: Any) -> MongoToolHandlers:
        settings = Settings.model_validate({
            "connections": {"m": {
                "url": mongo_url, "dialect": "mongodb", "rate_limit_per_min": 600,
                "statement_timeout_s": 10, "max_rows": 100, "mongo": mongo,
            }},
            "audit": {"path": str(tmp_path / "audit.jsonl"), "rotate_mb": 1},
        })
        mgr = MongoClientManager(settings)
        managers.append(mgr)
        return MongoToolHandlers(
            ConnectionManager(settings), mgr, RateLimiter(settings),
            AuditLogger(settings.audit.path, 1),
        )

    yield build
    for mgr in managers:
        mgr.close_all()


# ---- collections, views, validators -----------------------------------------


def test_list_tables_reports_views_and_hides_system_namespaces(make) -> None:
    kinds = {t["name"]: t["type"] for t in make().list_tables("m")}
    assert kinds["users"] == "collection"
    assert kinds["active_users"] == "view"
    assert not any(name.startswith("system.") for name in kinds)


def test_describe_table_returns_validator_and_type(make) -> None:
    res = make().describe_table("m", "customers")
    assert res["type"] == "collection"
    assert res["validator"]["$jsonSchema"]["required"] == ["name"]


def test_describe_view_has_no_indexes(make) -> None:
    res = make().describe_table("m", "active_users")
    assert res["type"] == "view" and res["indexes"] == []
    assert res["sample_size"] == 2  # two active users


def test_profile_flags_type_drift(make) -> None:
    (phone,) = make().profile_table("m", "customers", columns=["phone"])["fields"]
    assert phone["type_drift"] is True
    assert set(phone["types"]) == {"string", "int"}


# ---- database allowlist ------------------------------------------------------


def test_allowlisted_database_is_queryable_and_audited(make, tmp_path) -> None:
    h = make(databases=["dbread_extra"])
    assert {t["name"] for t in h.list_tables("m", database="dbread_extra")} == {"events"}
    res = h.query("m", {"countDocuments": "events", "filter": {}}, database="dbread_extra")
    assert res["rows"] == [[2]]
    last = json.loads((tmp_path / "audit.jsonl").read_text("utf-8").splitlines()[-1])
    assert json.loads(last["sql"])["$db"] == "dbread_extra"


def test_database_outside_allowlist_is_refused(make) -> None:
    with pytest.raises(ToolError, match="database_not_allowed: dbread_extra"):
        make().query("m", {"find": "events"}, database="dbread_extra")


def test_star_lists_every_readable_database_but_not_system_ones(make) -> None:
    h = make(databases=["*"], read_preference="primaryPreferred")
    names = h.list_schemas("m")
    assert {"dbread_test", "dbread_extra"} <= set(names)
    assert not {"admin", "local", "config"} & set(names)
    sizes = {d["name"]: d for d in h.list_databases("m")}
    assert isinstance(sizes["dbread_test"]["size_bytes"], int)


# ---- stats, overview, search, snapshot ---------------------------------------


def test_collection_stats_reports_sizes_without_index_stats_privilege(make) -> None:
    res = make().collection_stats("m", "users")
    assert res["count"] == 3 and res["size_bytes"] > 0
    by_name = {i["name"]: i for i in res["indexes"]}
    assert {"_id_", "email_1"} <= set(by_name)
    assert all(i["size_bytes"] > 0 for i in res["indexes"])
    # `read` grants no `indexStats` action, so usage is reported missing, not faked.
    assert "index_usage_error" in res
    assert all(i["ops"] is None for i in res["indexes"])


def test_database_overview(make) -> None:
    res = make().database_overview("m")
    assert res["schema"] == "dbread_test" and res["source"] == "sampled"
    tables = {t["name"]: t for t in res["tables"]}
    assert tables["users"]["row_estimate"] == 3
    assert tables["users"]["primary_key"] == ["_id"]
    assert tables["users"]["columns"] >= 5
    assert tables["users"]["size_bytes"] > 0
    assert tables["active_users"]["row_estimate"] is None


def test_search_schema_finds_tables_and_fields(make) -> None:
    res = make().search_schema("m", "USER")
    found = {(m["table"], m["column"], m["kind"]) for m in res["matches"]}
    assert ("users", None, "table") in found
    assert ("orders", "user_id", "column") in found
    assert res["truncated"] is False


def test_schema_snapshot(make) -> None:
    tables = make().schema_snapshot("m")["tables"]
    users = tables["users"]
    assert users["columns"]["email"] == {"type": "string", "nullable": False}
    assert users["columns"]["tags"]["nullable"] is True
    assert users["indexes"]["email_1"] == {"columns": ["email"], "unique": True}
    assert tables["customers"]["columns"]["phone"]["type"] == "mixed"


# ---- integrity ---------------------------------------------------------------


def test_check_integrity_finds_seeded_problems(make) -> None:
    res = make().check_integrity(
        "m", "reviews",
        references=[{"field": "user_id", "to": "users"}],
        unique_columns=["user_id"],
    )
    checks = {c["check"]: c for c in res["checks"]}
    assert all(c["error"] is None for c in res["checks"])
    assert checks["orphans"]["violations"] == 1
    assert checks["orphans"]["examples"][0]["user_id"] == 42
    assert checks["duplicates"]["violations"] == 1
    assert checks["duplicates"]["examples"][0] == {"user_id": 1, "duplicate_count": 2}
    assert checks["null_columns"]["examples"] == [{"column": "legacy"}]
    assert checks["constant_columns"]["examples"] == [{"column": "source", "value": "web"}]


def test_check_integrity_unknown_reference_target_is_a_check_error(make) -> None:
    res = make().check_integrity(
        "m", "reviews", checks=["orphans"],
        references=[{"field": "user_id", "to": "no_such_collection"}],
    )
    (check,) = res["checks"]
    assert check["error"] == "unknown_table: no_such_collection"
