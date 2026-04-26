"""Shared types for connection-string parsing layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from dbread.config import Dialect

# Supported input formats
Format = Literal["jdbc", "uri", "adonet", "odbc", "cloud", "filepath", "manual"]


@dataclass
class ParsedConn:
    """Normalised representation of any supported connection string format."""

    format: Format
    dialect: Dialect
    host: str | None = None
    port: int | None = None
    database: str | None = None
    user: str | None = None
    password: str | None = None
    params: dict[str, str] = field(default_factory=dict)
    raw: str = ""


class UnsupportedConnString(Exception):  # noqa: N818 — name mandated by spec
    """Raised for inputs we detect but deliberately cannot convert.

    Attributes
    ----------
    hint : str
        Human-readable action the user should take.
    """

    def __init__(self, msg: str, hint: str) -> None:
        super().__init__(msg)
        self.hint = hint


class UnknownFormat(Exception):  # noqa: N818 — name mandated by spec
    """Raised when the detector cannot identify any known format."""
