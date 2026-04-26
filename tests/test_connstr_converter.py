"""Unit tests for dbread.connstr.converter — ParsedConn → SQLAlchemy URL.

Coverage targets:
- All 8 dialects (postgres, mysql, mssql, oracle, sqlite, duckdb, clickhouse, mongodb)
- Password escaping: 10 nasty characters
- Default port application
- File-path forms: :memory:, relative, Unix absolute, Windows absolute
- MotherDuck (md:) cloud DuckDB
- MongoDB: standard and +srv
- ClickHouse Cloud: secure flag → port 8443
- MSSQL ODBC driver param
- Oracle SID in query params
- Edge cases: empty password, None password, empty params
"""

from __future__ import annotations

import urllib.parse

import pytest

from dbread.connstr.converter import (
    DEFAULT_PORT,
    DRIVER_SUFFIX,
    to_sqlalchemy_url,
)
from dbread.connstr.types import ParsedConn

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make(dialect, **kwargs) -> ParsedConn:
    """Convenience factory — defaults format to 'uri'."""
    return ParsedConn(format=kwargs.pop("format", "uri"), dialect=dialect, **kwargs)


# ---------------------------------------------------------------------------
# 1. DRIVER_SUFFIX / DEFAULT_PORT tables are complete
# ---------------------------------------------------------------------------


class TestTables:
    def test_driver_suffix_keys(self):
        expected = {
            "postgres", "mysql", "mssql", "oracle",
            "sqlite", "duckdb", "clickhouse", "mongodb",
        }
        assert set(DRIVER_SUFFIX.keys()) == expected

    def test_default_port_keys(self):
        # sqlite/duckdb/oracle have no default port in DEFAULT_PORT (file or SID path)
        for d in ("postgres", "mysql", "mssql", "clickhouse", "mongodb"):
            assert d in DEFAULT_PORT

    def test_driver_suffix_postgres(self):
        assert DRIVER_SUFFIX["postgres"] == "postgresql+psycopg2"

    def test_driver_suffix_mssql(self):
        assert DRIVER_SUFFIX["mssql"] == "mssql+pyodbc"


# ---------------------------------------------------------------------------
# 2. SQL-family dialects — roundtrip via urlparse
# ---------------------------------------------------------------------------


class TestSqlDialectsRoundtrip:
    def test_postgres_basic(self):
        p = _make("postgres", host="localhost", port=5432, database="mydb",
                  user="scott", password="tiger")
        url = to_sqlalchemy_url(p)
        parsed = urllib.parse.urlparse(url)
        assert parsed.hostname == "localhost"
        assert parsed.port == 5432
        assert parsed.username == "scott"
        assert urllib.parse.unquote(parsed.password) == "tiger"
        assert "mydb" in parsed.path

    def test_mysql_basic(self):
        p = _make("mysql", host="db.example.com", port=3306, database="shop",
                  user="root", password="rootpw")
        url = to_sqlalchemy_url(p)
        assert url.startswith("mysql+pymysql://")
        parsed = urllib.parse.urlparse(url)
        assert parsed.hostname == "db.example.com"
        assert parsed.port == 3306

    def test_mssql_basic(self):
        p = _make("mssql", host="sqlhost", port=1433, database="testdb",
                  user="sa", password="Secret1")
        url = to_sqlalchemy_url(p)
        assert url.startswith("mssql+pyodbc://")
        parsed = urllib.parse.urlparse(url)
        assert parsed.hostname == "sqlhost"

    def test_oracle_basic(self):
        p = _make("oracle", host="orahost", port=1521, database="ORCL_SERVICE",
                  user="hr", password="hrpw")
        url = to_sqlalchemy_url(p)
        assert url.startswith("oracle+oracledb://")
        parsed = urllib.parse.urlparse(url)
        assert parsed.port == 1521

    def test_clickhouse_basic(self):
        p = _make("clickhouse", host="ch.host", port=8123, database="default",
                  user="default", password="")
        url = to_sqlalchemy_url(p)
        assert url.startswith("clickhouse+http://")
        parsed = urllib.parse.urlparse(url)
        assert parsed.hostname == "ch.host"

    def test_postgres_query_params_preserved(self):
        p = _make("postgres", host="host", port=5432, database="db",
                  user="u", password="p", params={"sslmode": "require"})
        url = to_sqlalchemy_url(p)
        assert "sslmode=require" in url

    def test_mysql_query_params_preserved(self):
        p = _make("mysql", host="host", port=3306, database="db",
                  user="u", password="p", params={"useSSL": "false", "charset": "utf8mb4"})
        url = to_sqlalchemy_url(p)
        assert "charset=utf8mb4" in url


# ---------------------------------------------------------------------------
# 3. Default port applied when port is None
# ---------------------------------------------------------------------------


class TestDefaultPort:
    def test_postgres_default_port(self):
        p = _make("postgres", host="host", port=None, database="db", user="u", password="p")
        url = to_sqlalchemy_url(p)
        parsed = urllib.parse.urlparse(url)
        assert parsed.port == DEFAULT_PORT["postgres"]

    def test_mysql_default_port(self):
        p = _make("mysql", host="host", port=None, database="db", user="u", password="p")
        url = to_sqlalchemy_url(p)
        parsed = urllib.parse.urlparse(url)
        assert parsed.port == DEFAULT_PORT["mysql"]

    def test_mssql_default_port(self):
        p = _make("mssql", host="host", port=None, database="db", user="u", password="p")
        url = to_sqlalchemy_url(p)
        parsed = urllib.parse.urlparse(url)
        assert parsed.port == DEFAULT_PORT["mssql"]

    def test_clickhouse_default_port(self):
        p = _make("clickhouse", host="host", port=None, database="db", user="u", password="p")
        url = to_sqlalchemy_url(p)
        parsed = urllib.parse.urlparse(url)
        assert parsed.port == DEFAULT_PORT["clickhouse"]


# ---------------------------------------------------------------------------
# 4. Password escaping — 10 nasty characters
# ---------------------------------------------------------------------------


NASTY_PASSWORDS = [
    "p@ss",
    "a:b",
    "c/d",
    "e+f",
    "g%h",
    "i#j",
    "k?l",
    "m&n",
    "o=p",
    "s pace",
]


class TestPasswordEscaping:
    @pytest.mark.parametrize("raw_pw", NASTY_PASSWORDS)
    def test_password_roundtrip(self, raw_pw):
        """URL.create must escape special chars; urlparse + unquote recovers original."""
        p = _make("postgres", host="localhost", port=5432, database="db",
                  user="user", password=raw_pw)
        url = to_sqlalchemy_url(p)
        parsed = urllib.parse.urlparse(url)
        # password in the netloc is percent-encoded; unquote recovers raw
        recovered = urllib.parse.unquote(parsed.password)
        assert recovered == raw_pw, f"password {raw_pw!r} did not roundtrip via {url!r}"

    def test_none_password(self):
        p = _make("postgres", host="localhost", port=5432, database="db",
                  user="user", password=None)
        url = to_sqlalchemy_url(p)
        parsed = urllib.parse.urlparse(url)
        # no password section should appear in netloc
        assert parsed.password is None

    def test_empty_password(self):
        p = _make("postgres", host="localhost", port=5432, database="db",
                  user="user", password="")
        # empty string password must not raise
        url = to_sqlalchemy_url(p)
        assert "user" in url


# ---------------------------------------------------------------------------
# 5. SQLite / DuckDB file-path forms
# ---------------------------------------------------------------------------


class TestFilepathUrls:
    def test_sqlite_memory(self):
        p = _make("sqlite", format="filepath", database=":memory:")
        assert to_sqlalchemy_url(p) == "sqlite:///:memory:"

    def test_duckdb_memory(self):
        p = _make("duckdb", format="filepath", database=":memory:")
        assert to_sqlalchemy_url(p) == "duckdb:///:memory:"

    def test_sqlite_relative_path(self):
        p = _make("sqlite", format="filepath", database="my.db")
        assert to_sqlalchemy_url(p) == "sqlite:///my.db"

    def test_sqlite_unix_absolute(self):
        p = _make("sqlite", format="filepath", database="/var/data/my.db")
        # 4 slashes total: "sqlite://" + "/" + "/var/data/my.db"
        assert to_sqlalchemy_url(p) == "sqlite:////var/data/my.db"

    def test_sqlite_windows_absolute_forward_slash(self):
        # Parser pre-converts backslashes; we receive forward-slash path
        p = _make("sqlite", format="filepath", database="C:/data/my.db")
        assert to_sqlalchemy_url(p) == "sqlite:///C:/data/my.db"

    def test_sqlite_windows_absolute_backslash_converted(self):
        # Converter itself also handles any residual backslashes
        p = _make("sqlite", format="filepath", database="C:\\data\\my.db")
        result = to_sqlalchemy_url(p)
        assert result == "sqlite:///C:/data/my.db"
        assert "\\" not in result

    def test_duckdb_relative(self):
        p = _make("duckdb", format="filepath", database="analytics.duckdb")
        assert to_sqlalchemy_url(p) == "duckdb:///analytics.duckdb"

    def test_duckdb_unix_absolute(self):
        p = _make("duckdb", format="filepath", database="/home/user/data.duckdb")
        assert to_sqlalchemy_url(p) == "duckdb:////home/user/data.duckdb"


# ---------------------------------------------------------------------------
# 6. MotherDuck (DuckDB cloud)
# ---------------------------------------------------------------------------


class TestMotherDuck:
    def test_motherduck_with_token(self):
        p = ParsedConn(
            format="cloud",
            dialect="duckdb",
            database="md:mydb",
            params={"motherduck_token": "tok123"},
        )
        result = to_sqlalchemy_url(p)
        assert result.startswith("duckdb:///md:mydb")
        assert "motherduck_token=tok123" in result

    def test_motherduck_no_token(self):
        p = ParsedConn(format="cloud", dialect="duckdb", database="md:mydb")
        result = to_sqlalchemy_url(p)
        assert result == "duckdb:///md:mydb"

    def test_motherduck_empty_dbname(self):
        p = ParsedConn(format="cloud", dialect="duckdb", database="md:")
        result = to_sqlalchemy_url(p)
        assert result == "duckdb:///md:"


# ---------------------------------------------------------------------------
# 7. MongoDB
# ---------------------------------------------------------------------------


class TestMongoDb:
    def test_mongodb_standard(self):
        p = _make("mongodb", host="mongo.host", port=27017, database="mydb",
                  user="admin", password="pass")
        url = to_sqlalchemy_url(p)
        assert url.startswith("mongodb://")
        assert "admin" in url
        assert "mongo.host:27017" in url
        assert "/mydb" in url

    def test_mongodb_default_port(self):
        p = _make("mongodb", host="mongo.host", port=None, database="mydb",
                  user="admin", password="pass")
        url = to_sqlalchemy_url(p)
        assert f"mongo.host:{DEFAULT_PORT['mongodb']}" in url

    def test_mongodb_srv(self):
        p = ParsedConn(
            format="cloud",
            dialect="mongodb",
            host="cluster0.abc123.mongodb.net",
            database="mydb",
            user="user",
            password="pass",
            params={"srv": "true", "retryWrites": "true"},
        )
        url = to_sqlalchemy_url(p)
        assert url.startswith("mongodb+srv://")
        # SRV must not include port
        assert ":27017" not in url
        # 'srv' key must not appear in query string
        assert "srv=true" not in url
        assert "retryWrites=true" in url
        assert "/mydb" in url

    def test_mongodb_srv_password_escaped(self):
        p = ParsedConn(
            format="cloud",
            dialect="mongodb",
            host="cluster0.example.net",
            database="db",
            user="user",
            password="p@ss/word",
            params={"srv": "true"},
        )
        url = to_sqlalchemy_url(p)
        # raw password must NOT appear literally
        assert "p@ss/word" not in url
        # percent-encoded form must be present
        assert "p%40ss" in url

    def test_mongodb_no_auth(self):
        p = _make("mongodb", host="localhost", port=27017, database="admin",
                  user=None, password=None)
        url = to_sqlalchemy_url(p)
        assert "@" not in url
        assert url.startswith("mongodb://localhost:27017/admin")

    def test_mongodb_user_no_password(self):
        p = _make("mongodb", host="host", port=27017, database="db",
                  user="onlyuser", password=None)
        url = to_sqlalchemy_url(p)
        assert "onlyuser@" in url
        assert ":@" not in url


# ---------------------------------------------------------------------------
# 8. ClickHouse Cloud: secure flag → port 8443
# ---------------------------------------------------------------------------


class TestClickHouseCloud:
    def test_secure_flag_sets_default_port_8443(self):
        p = _make("clickhouse", host="abc.clickhouse.cloud", port=None,
                  database="default", user="default", password="token",
                  params={"secure": "true"})
        url = to_sqlalchemy_url(p)
        parsed = urllib.parse.urlparse(url)
        assert parsed.port == 8443

    def test_explicit_port_not_overridden_by_secure(self):
        p = _make("clickhouse", host="abc.clickhouse.cloud", port=9999,
                  database="default", user="default", password="token",
                  params={"secure": "true"})
        url = to_sqlalchemy_url(p)
        parsed = urllib.parse.urlparse(url)
        assert parsed.port == 9999

    def test_no_secure_flag_uses_default_8123(self):
        p = _make("clickhouse", host="host", port=None, database="db",
                  user="u", password="p")
        url = to_sqlalchemy_url(p)
        parsed = urllib.parse.urlparse(url)
        assert parsed.port == 8123


# ---------------------------------------------------------------------------
# 9. MSSQL ODBC driver param injection
# ---------------------------------------------------------------------------


class TestMssqlOdbc:
    def test_odbc_driver_in_query(self):
        p = ParsedConn(
            format="odbc",
            dialect="mssql",
            host="sqlhost",
            port=1433,
            database="mydb",
            user="sa",
            password="pw",
            params={"driver": "ODBC Driver 17 for SQL Server"},
        )
        url = to_sqlalchemy_url(p)
        # SQLAlchemy URL.create encodes spaces as + in query strings
        assert "driver" in url
        assert "ODBC" in url

    def test_odbc_driver_with_encrypt(self):
        p = ParsedConn(
            format="odbc",
            dialect="mssql",
            host="host",
            port=1433,
            database="db",
            user="sa",
            password="pw",
            params={"driver": "ODBC Driver 18 for SQL Server", "Encrypt": "yes"},
        )
        url = to_sqlalchemy_url(p)
        assert url.startswith("mssql+pyodbc://")
        assert "Encrypt" in url or "encrypt" in url


# ---------------------------------------------------------------------------
# 10. Oracle SID vs SERVICE_NAME
# ---------------------------------------------------------------------------


class TestOracle:
    def test_oracle_sid_in_query(self):
        p = ParsedConn(
            format="jdbc",
            dialect="oracle",
            host="orahost",
            port=1521,
            database="MYORCL",
            user="hr",
            password="hrpw",
            params={"sid": "MYORCL"},
        )
        url = to_sqlalchemy_url(p)
        assert "sid=MYORCL" in url

    def test_oracle_service_name_as_database(self):
        p = ParsedConn(
            format="jdbc",
            dialect="oracle",
            host="orahost",
            port=1521,
            database="ORCL_SERVICE",
            user="hr",
            password="hrpw",
            params={"service_name": "ORCL_SERVICE"},
        )
        url = to_sqlalchemy_url(p)
        assert "ORCL_SERVICE" in url


# ---------------------------------------------------------------------------
# 11. Empty params dict
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_params_no_question_mark(self):
        p = _make("postgres", host="host", port=5432, database="db",
                  user="u", password="p", params={})
        url = to_sqlalchemy_url(p)
        assert "?" not in url

    def test_sqlite_empty_params(self):
        p = _make("sqlite", format="filepath", database="my.db", params={})
        url = to_sqlalchemy_url(p)
        assert "?" not in url

    def test_mongodb_empty_params(self):
        p = _make("mongodb", host="host", port=27017, database="db",
                  user="u", password="p", params={})
        url = to_sqlalchemy_url(p)
        assert "?" not in url
