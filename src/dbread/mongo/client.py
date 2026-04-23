"""MongoClient lifecycle manager — one cached client per configured connection."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from ..config import Settings

if TYPE_CHECKING:
    from pymongo import MongoClient
    from pymongo.database import Database

log = logging.getLogger("dbread.mongo.client")


def _warn_mongo_tls(name: str, url: str) -> None:
    """Warn once if the URI has no TLS hint (matches SQL connections behavior)."""
    lower = url.lower()
    if lower.startswith("mongodb+srv://"):
        return
    if "tls=true" in lower or "ssl=true" in lower:
        return
    log.warning(
        "connection %r (mongodb) has no TLS hint (tls=true|ssl=true|mongodb+srv); "
        "credentials may travel plaintext",
        name,
    )


class MongoClientManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._clients: dict[str, MongoClient] = {}

    def get_client(self, name: str) -> MongoClient:
        cached = self._clients.get(name)
        if cached is not None:
            return cached
        cfg = self.settings.connections.get(name)
        if cfg is None or cfg.dialect != "mongodb":
            raise KeyError(f"unknown mongo connection: {name!r}")
        url = cfg.resolved_url()
        _warn_mongo_tls(name, url)

        from pymongo import MongoClient

        client = MongoClient(
            url,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=max(1, cfg.statement_timeout_s) * 1000,
            appname="dbread",
        )
        self._clients[name] = client
        return client

    def get_db(self, name: str) -> Database:
        client = self.get_client(name)
        cfg = self.settings.connections[name]
        db_name = urlparse(cfg.resolved_url()).path.lstrip("/") or "test"
        return client[db_name]

    def close_all(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()
