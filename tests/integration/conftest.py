"""Integration test fixtures.

PG and MySQL tests skip gracefully if Docker is unavailable. SQLite tests
always run. To spin up PG+MySQL manually:

    cd tests/integration
    docker compose up -d

Then run `pytest tests/integration/`.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import time
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text

from dbread.audit import AuditLogger
from dbread.config import AuditConfig, ConnectionConfig, Settings
from dbread.connections import ConnectionManager
from dbread.rate_limiter import RateLimiter
from dbread.sql_guard import SqlGuard
from dbread.tools import ToolHandlers

HERE = pathlib.Path(__file__).parent
COMPOSE = HERE / "docker-compose.yml"

PG_URL = "postgresql+psycopg2://ai_readonly:ropw@localhost:54329/testdb"
MYSQL_URL = "mysql+pymysql://ai_readonly:ropw@localhost:33069/testdb"
CLICKHOUSE_URL = "clickhouse+http://ai_readonly:ropw@localhost:81239/testdb"
MONGO_URL = os.environ.get(
    "MONGO_URL",
    "mongodb://ai_ro:ro_pw@localhost:27019/dbread_test?authSource=dbread_test",
)


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _compose(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), *args],
        check=True,
        cwd=HERE,
    )


def _wait_ready(url: str, timeout: int = 60) -> None:
    engine = create_engine(url)
    deadline = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < deadline:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return
        except Exception as e:
            last_err = e
            time.sleep(1)
    engine.dispose()
    raise TimeoutError(f"DB not ready at {url}: {last_err}")


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    if os.environ.get("SKIP_DOCKER") or not _docker_available():
        pytest.skip("docker unavailable or SKIP_DOCKER set")
    _compose("up", "-d", "pg")
    try:
        _wait_ready(PG_URL)
        yield PG_URL
    finally:
        if os.environ.get("KEEP_CONTAINERS") != "1":
            _compose("down", "-v")


@pytest.fixture(scope="session")
def mysql_url() -> Iterator[str]:
    if os.environ.get("SKIP_DOCKER") or not _docker_available():
        pytest.skip("docker unavailable or SKIP_DOCKER set")
    _compose("up", "-d", "mysql")
    _wait_ready(MYSQL_URL, timeout=120)  # MySQL init takes longer
    yield MYSQL_URL


@pytest.fixture(scope="session")
def clickhouse_url() -> Iterator[str]:
    if os.environ.get("SKIP_DOCKER") or not _docker_available():
        pytest.skip("docker unavailable or SKIP_DOCKER set")
    _compose("up", "-d", "clickhouse")
    _wait_ready(CLICKHOUSE_URL, timeout=60)
    yield CLICKHOUSE_URL


def _mongo_reachable(url: str, timeout: int = 30) -> bool:
    try:
        from pymongo import MongoClient
    except ImportError:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            client = MongoClient(url, serverSelectionTimeoutMS=1000)
            client.admin.command("ping")
            client.close()
            return True
        except Exception:
            time.sleep(1)
    return False


@pytest.fixture(scope="session")
def mongo_url() -> Iterator[str]:
    """Reach an existing Mongo (via MONGO_URL) or spin one up via docker compose.

    Skips gracefully if docker is unavailable and the env var does not point
    at a reachable Mongo (Windows CI etc).
    """
    env_url = os.environ.get("MONGO_URL")
    if env_url and _mongo_reachable(env_url, timeout=5):
        yield env_url
        return
    if os.environ.get("SKIP_DOCKER") or not _docker_available():
        pytest.skip("docker unavailable or SKIP_DOCKER set")
    _compose("up", "-d", "mongo")
    if not _mongo_reachable(MONGO_URL, timeout=60):
        pytest.skip("mongo container did not become reachable")
    yield MONGO_URL


def build_mongo_handlers(url: str, tmp_path: pathlib.Path):
    """Bootstrap ToolHandlers wired for a mongodb dialect connection."""
    from dbread.mongo.client import MongoClientManager
    from dbread.mongo.tools import MongoToolHandlers

    settings = Settings(
        connections={
            "m": ConnectionConfig(
                url=url,
                dialect="mongodb",
                rate_limit_per_min=600,
                statement_timeout_s=10,
                max_rows=100,
            ),
        },
        audit=AuditConfig(path=str(tmp_path / "audit.jsonl"), rotate_mb=1),
    )
    cm = ConnectionManager(settings)
    rl = RateLimiter(settings)
    audit = AuditLogger(settings.audit.path, 1)
    mongo_mgr = MongoClientManager(settings)
    mongo_handlers = MongoToolHandlers(cm, mongo_mgr, rl, audit)
    handlers = ToolHandlers(
        settings=settings,
        conn_mgr=cm,
        guard=SqlGuard(),
        rate_limiter=rl,
        audit=audit,
        mongo=mongo_handlers,
    )
    return handlers, mongo_mgr


def build_handlers(
    url: str,
    dialect: str,
    tmp_path: pathlib.Path,
    *,
    rate_per_min: int = 60,
    max_rows: int = 100,
) -> ToolHandlers:
    settings = Settings(
        connections={
            "t": ConnectionConfig(
                url=url,
                dialect=dialect,  # type: ignore[arg-type]
                rate_limit_per_min=rate_per_min,
                statement_timeout_s=5,
                max_rows=max_rows,
            ),
        },
        audit=AuditConfig(path=str(tmp_path / "audit.jsonl"), rotate_mb=1),
    )
    cm = ConnectionManager(settings)
    return ToolHandlers(
        settings=settings,
        conn_mgr=cm,
        guard=SqlGuard(),
        rate_limiter=RateLimiter(settings),
        audit=AuditLogger(settings.audit.path, 1),
    )
