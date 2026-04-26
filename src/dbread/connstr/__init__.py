"""Connection-string detection and parsing package."""

from dbread.connstr.detector import detect_and_parse
from dbread.connstr.types import Format, ParsedConn, UnknownFormat, UnsupportedConnString

__all__ = [
    "detect_and_parse",
    "Format",
    "ParsedConn",
    "UnknownFormat",
    "UnsupportedConnString",
]
