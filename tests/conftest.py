"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.fixture
def sqlite_config_yaml(tmp_path: Path) -> Path:
    """Write a minimal config.yaml with a SQLite in-memory connection."""
    cfg = {
        "connections": {
            "mem": {
                "url": "sqlite:///:memory:",
                "dialect": "sqlite",
                "rate_limit_per_min": 60,
                "statement_timeout_s": 30,
                "max_rows": 1000,
            },
        },
        "audit": {"path": str(tmp_path / "audit.jsonl"), "rotate_mb": 50},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path
