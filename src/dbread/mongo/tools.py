"""MongoDB tool handlers for the `mongodb` dialect.

The behaviour lives in focused modules; this class only assembles them:

- `base.py`      database allowlist, metadata reads, audit
- `query.py`     guarded `query` / `explain` (every data read goes here)
- `inspect.py`   list/describe/profile a collection, list databases, stats
- `catalog.py`   database overview, schema search, schema snapshot
- `integrity.py` orphan / duplicate / null / constant checks

Every public method takes `database: str | None = None` last; `None` means
the database named in the connection URI.
"""

from __future__ import annotations

from .catalog import MongoCatalogMixin
from .execute import HANDLED_FIELDS
from .inspect import MongoInspectMixin
from .integrity import MongoIntegrityMixin

__all__ = ["HANDLED_FIELDS", "MongoToolHandlers"]


class MongoToolHandlers(MongoInspectMixin, MongoCatalogMixin, MongoIntegrityMixin):
    """Read-only MongoDB tools: introspection, guarded queries, integrity checks."""
