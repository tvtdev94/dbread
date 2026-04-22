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
