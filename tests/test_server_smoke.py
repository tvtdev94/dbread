"""Subprocess smoke test for MCP server over stdio.

Spawns `python -m dbread.server` and exchanges JSON-RPC frames to verify
initialize handshake, tools/list, and tools/call for list_connections.
Covers the server.py wiring that pytest in-process can't reach.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.timeout(15)


@pytest.fixture
def smoke_config(tmp_path: Path) -> Path:
    db_path = tmp_path / "smoke.db"
    import sqlite3
    with sqlite3.connect(db_path) as con:
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("INSERT INTO t(name) VALUES ('alpha'), ('beta')")
    cfg = {
        "connections": {
            "smoke": {
                "url": f"sqlite:///{db_path.as_posix()}",
                "dialect": "sqlite",
                "rate_limit_per_min": 60,
                "statement_timeout_s": 10,
                "max_rows": 100,
            }
        },
        "audit": {"path": str(tmp_path / "audit.jsonl"), "rotate_mb": 50},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Server:
    def __init__(self, config_path: Path) -> None:
        env = {
            **os.environ,
            "DBREAD_CONFIG": str(config_path),
            "PYTHONUNBUFFERED": "1",
            # Enable subprocess coverage so server.py is measured.
            # The coverage .pth file picks this up on interpreter start.
            "COVERAGE_PROCESS_START": str(_PROJECT_ROOT / "pyproject.toml"),
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "dbread.server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            encoding="utf-8",
            bufsize=1,
        )
        self._id = 0

    def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        frame = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            frame["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        return self._read_response(self._id)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        frame: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            frame["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()

    def _read_response(self, wanted_id: int, timeout_s: float = 10.0) -> dict[str, Any]:
        assert self.proc.stdout is not None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == wanted_id:
                return msg
        raise TimeoutError(f"no response for id={wanted_id}; stderr={self._stderr_tail()}")

    def _stderr_tail(self) -> str:
        try:
            assert self.proc.stderr is not None
            return self.proc.stderr.read() or ""
        except Exception:
            return ""

    def close(self) -> None:
        # Graceful shutdown via stdin EOF lets the server run atexit handlers
        # (important so coverage of subprocess is flushed on Windows, where
        # terminate() == TerminateProcess() == SIGKILL).
        try:
            if self.proc.stdin is not None and not self.proc.stdin.closed:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)


@pytest.fixture
def server(smoke_config: Path):
    s = Server(smoke_config)
    try:
        yield s
    finally:
        s.close()


def _do_initialize(server: Server) -> dict[str, Any]:
    resp = server.send(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "0.1"},
        },
    )
    server.notify("notifications/initialized")
    return resp


def test_initialize_handshake(server: Server) -> None:
    resp = _do_initialize(server)
    assert "result" in resp, resp
    info = resp["result"].get("serverInfo", {})
    assert info.get("name") == "dbread"


def test_list_tools(server: Server) -> None:
    _do_initialize(server)
    resp = server.send("tools/list")
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {"list_connections", "list_tables", "describe_table", "query", "explain"}


def test_call_list_connections(server: Server) -> None:
    _do_initialize(server)
    resp = server.send(
        "tools/call",
        {"name": "list_connections", "arguments": {}},
    )
    content = resp["result"]["content"]
    payload = json.loads(content[0]["text"])
    assert isinstance(payload, list)
    assert payload == [{"name": "smoke", "dialect": "sqlite"}]


def test_call_query_runs_under_to_thread(server: Server) -> None:
    """query path should succeed end-to-end via asyncio.to_thread wrap."""
    _do_initialize(server)
    resp = server.send(
        "tools/call",
        {
            "name": "query",
            "arguments": {
                "connection": "smoke",
                "sql": "SELECT count(*) AS n FROM t",
            },
        },
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload.get("columns") == ["n"]
    assert payload.get("rows") == [[2]]
