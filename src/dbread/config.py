"""Typed configuration loaded from YAML + environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator, model_validator

Dialect = Literal[
    "postgres", "mysql", "mssql", "sqlite", "oracle", "duckdb", "clickhouse",
]


class ConnectionConfig(BaseModel):
    url: str | None = None
    url_env: str | None = None
    dialect: Dialect
    rate_limit_per_min: int = 60
    statement_timeout_s: int = 30
    max_rows: int = 1000

    @model_validator(mode="after")
    def _check_url(self) -> ConnectionConfig:
        if bool(self.url) == bool(self.url_env):
            raise ValueError("exactly one of 'url' or 'url_env' must be set")
        return self

    def resolved_url(self) -> str:
        if self.url:
            return self.url
        assert self.url_env is not None
        value = os.environ.get(self.url_env)
        if not value:
            raise ValueError(f"environment variable {self.url_env!r} is not set")
        return value


class AuditConfig(BaseModel):
    path: str = "./audit.jsonl"
    rotate_mb: int = 50
    timezone: str = "UTC"
    redact_literals: bool = False

    @field_validator("path")
    @classmethod
    def _expand_path(cls, v: str) -> str:
        return str(Path(v).expanduser())


class Settings(BaseModel):
    connections: dict[str, ConnectionConfig]
    audit: AuditConfig = AuditConfig()
    global_rate_limit_per_min: int | None = None

    @field_validator("global_rate_limit_per_min")
    @classmethod
    def _check_global_rate(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("global_rate_limit_per_min must be > 0 if set")
        return v

    @model_validator(mode="after")
    def _check_connections(self) -> Settings:
        if not self.connections:
            raise ValueError("at least one connection required")
        return self

    @classmethod
    def load(cls, path: str | Path = "config.yaml") -> Settings:
        resolved = Path(path).expanduser()
        with open(resolved, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls(**raw)
