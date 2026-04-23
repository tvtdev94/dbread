"""Unit tests for MongoClientManager — no real MongoDB required."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dbread.config import MongoConfig, Settings
from dbread.mongo.client import MongoClientManager, _warn_mongo_tls


def _settings(dialect: str = "mongodb", url: str = "mongodb://u:p@h:27017/testdb") -> Settings:
    return Settings.model_validate({
        "connections": {
            "m": {
                "url": url,
                "dialect": dialect,
                "statement_timeout_s": 30,
                "rate_limit_per_min": 60,
                "max_rows": 1000,
                "mongo": {"sample_size": 100},
            },
        },
    })


def test_mongo_dialect_allowed() -> None:
    s = _settings()
    assert s.connections["m"].dialect == "mongodb"
    assert isinstance(s.connections["m"].mongo, MongoConfig)


def test_mongo_sample_size_range() -> None:
    with pytest.raises(Exception, match="sample_size"):
        MongoConfig(sample_size=5)
    with pytest.raises(Exception, match="sample_size"):
        MongoConfig(sample_size=5000)
    assert MongoConfig(sample_size=50).sample_size == 50


def test_manager_caches_client() -> None:
    s = _settings()
    mgr = MongoClientManager(s)
    fake = MagicMock()
    with patch("pymongo.MongoClient", return_value=fake) as mc:
        c1 = mgr.get_client("m")
        c2 = mgr.get_client("m")
    assert c1 is c2 is fake
    assert mc.call_count == 1


def test_manager_rejects_non_mongo() -> None:
    s = Settings.model_validate({
        "connections": {
            "s": {"url": "sqlite:///x.db", "dialect": "sqlite"},
        },
    })
    mgr = MongoClientManager(s)
    with pytest.raises(KeyError, match="unknown mongo connection"):
        mgr.get_client("s")


def test_manager_get_db_extracts_name() -> None:
    s = _settings(url="mongodb://u:p@h:27017/analytics?authSource=admin")
    mgr = MongoClientManager(s)
    fake = MagicMock()
    fake.__getitem__.return_value = "analytics_db_obj"
    with patch("pymongo.MongoClient", return_value=fake):
        db = mgr.get_db("m")
    fake.__getitem__.assert_called_once_with("analytics")
    assert db == "analytics_db_obj"


def test_manager_get_db_defaults_when_no_path() -> None:
    s = _settings(url="mongodb://u:p@h:27017/")
    mgr = MongoClientManager(s)
    fake = MagicMock()
    with patch("pymongo.MongoClient", return_value=fake):
        mgr.get_db("m")
    fake.__getitem__.assert_called_once_with("test")


def test_manager_close_all() -> None:
    s = _settings()
    mgr = MongoClientManager(s)
    fake = MagicMock()
    with patch("pymongo.MongoClient", return_value=fake):
        mgr.get_client("m")
    mgr.close_all()
    fake.close.assert_called_once()
    assert mgr._clients == {}


def test_tls_warning_triggers(caplog: pytest.LogCaptureFixture) -> None:
    import logging
    with caplog.at_level(logging.WARNING, logger="dbread.mongo.client"):
        _warn_mongo_tls("m", "mongodb://u:p@host:27017/db")
    assert any("no TLS hint" in r.message for r in caplog.records)


def test_tls_warning_suppressed_for_srv(caplog: pytest.LogCaptureFixture) -> None:
    import logging
    with caplog.at_level(logging.WARNING, logger="dbread.mongo.client"):
        _warn_mongo_tls("m", "mongodb+srv://u:p@cluster.mongodb.net/db")
    assert not any("no TLS hint" in r.message for r in caplog.records)


def test_tls_warning_suppressed_for_tls_param(caplog: pytest.LogCaptureFixture) -> None:
    import logging
    with caplog.at_level(logging.WARNING, logger="dbread.mongo.client"):
        _warn_mongo_tls("m", "mongodb://u:p@host:27017/db?tls=true")
    assert not any("no TLS hint" in r.message for r in caplog.records)
