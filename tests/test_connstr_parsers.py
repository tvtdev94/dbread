"""Unit tests for individual connection-string parsers (5+ per format family)."""

from __future__ import annotations

import pytest

from dbread.connstr.parsers import (
    adonet,  # noqa: I001
    cloud,
    filepath,
    jdbc,
    odbc,
    uri,
)
from dbread.connstr.types import UnknownFormat, UnsupportedConnString

# ---------------------------------------------------------------------------
# uri.py
# ---------------------------------------------------------------------------


class TestUriParser:
    def test_postgresql_happy_path(self):
        r = uri.parse("postgresql://scott:tiger@localhost:5432/mydb")
        assert r.dialect == "postgres"
        assert r.format == "uri"
        assert r.host == "localhost"
        assert r.port == 5432
        assert r.database == "mydb"
        assert r.user == "scott"
        assert r.password == "tiger"

    def test_postgres_alias(self):
        r = uri.parse("postgres://user:pwd@db.example.com/prod")
        assert r.dialect == "postgres"
        assert r.host == "db.example.com"
        assert r.database == "prod"

    def test_mysql_with_port(self):
        r = uri.parse("mysql://root:pass@127.0.0.1:3306/shop")
        assert r.dialect == "mysql"
        assert r.port == 3306
        assert r.database == "shop"

    def test_mssql_via_sqlserver_scheme(self):
        r = uri.parse("mssql://sa:Secret1@sqlhost:1433/testdb")
        assert r.dialect == "mssql"
        assert r.port == 1433

    def test_mongodb_standard(self):
        r = uri.parse("mongodb://admin:pass@mongo.host:27017/mydb")
        assert r.dialect == "mongodb"
        assert r.port == 27017
        assert r.database == "mydb"

    def test_mongodb_srv_sets_srv_param(self):
        r = uri.parse("mongodb+srv://user:pass@cluster0.abc123.mongodb.net/mydb?retryWrites=true")
        assert r.dialect == "mongodb"
        assert r.params.get("srv") == "true"
        assert r.params.get("retryWrites") == "true"

    def test_special_chars_in_password_preserved_raw(self):
        # Password is URL-encoded in the raw string; parser must decode it
        raw = "postgresql://user:p%40ss%3Aword@localhost/db"
        r = uri.parse(raw)
        assert r.password == "p@ss:word"
        assert r.raw == raw  # raw is preserved verbatim

    def test_driver_suffix_stripped_from_scheme(self):
        r = uri.parse("postgresql+psycopg2://user:pass@host/db")
        assert r.dialect == "postgres"

    def test_query_params_extracted(self):
        r = uri.parse("postgresql://user:pass@host/db?sslmode=require&connect_timeout=10")
        assert r.params["sslmode"] == "require"
        assert r.params["connect_timeout"] == "10"

    def test_sqlite_uri(self):
        r = uri.parse("sqlite:///data/my.db")
        assert r.dialect == "sqlite"
        assert r.database == "data/my.db"

    def test_duckdb_uri(self):
        r = uri.parse("duckdb:///myfile.duckdb")
        assert r.dialect == "duckdb"

    def test_oracle_tns_descriptor_raises(self):
        with pytest.raises(UnsupportedConnString) as exc_info:
            uri.parse("oracle://user:pass@host/(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)))")
        assert exc_info.value.hint

    def test_unknown_scheme_raises(self):
        with pytest.raises(ValueError, match="Unrecognised URI scheme"):
            uri.parse("ftp://user:pass@host/db")


# ---------------------------------------------------------------------------
# adonet.py
# ---------------------------------------------------------------------------


class TestAdonetParser:
    def test_postgres_style(self):
        r = adonet.parse(
            "Server=localhost;Port=5432;Database=mydb;User Id=scott;Password=tiger;",
            dialect_hint="postgres",
        )
        assert r.dialect == "postgres"
        assert r.host == "localhost"
        assert r.port == 5432
        assert r.database == "mydb"
        assert r.user == "scott"
        assert r.password == "tiger"

    def test_mssql_initial_catalog(self):
        r = adonet.parse(
            "Server=sqlhost;Initial Catalog=mydb;User Id=sa;Password=S3cr3t;"
        )
        assert r.dialect == "mssql"
        assert r.database == "mydb"

    def test_mssql_server_with_port_comma(self):
        r = adonet.parse(
            "Server=myhost,1433;Database=testdb;User Id=sa;Password=pw;"
        )
        assert r.dialect == "mssql"
        assert r.host == "myhost"
        assert r.port == 1433

    def test_quoted_password_with_semicolon(self):
        r = adonet.parse(
            'Server=host;Database=db;User Id=user;Password="pass;word";'
        )
        assert r.password == "pass;word"
        assert r.database == "db"

    def test_trusted_connection_blocks(self):
        with pytest.raises(UnsupportedConnString) as exc_info:
            adonet.parse("Server=host;Database=db;Trusted_Connection=yes;")
        assert "Windows authentication" in exc_info.value.hint

    def test_integrated_security_sspi_blocks(self):
        with pytest.raises(UnsupportedConnString):
            adonet.parse("Server=host;Database=db;Integrated Security=SSPI;")

    def test_named_instance_blocks(self):
        with pytest.raises(UnsupportedConnString) as exc_info:
            adonet.parse(r"Server=HOST\SQLEXPRESS;Database=db;User Id=sa;Password=pw;")
        assert "Named instance" in exc_info.value.hint or "named" in exc_info.value.hint.lower()

    def test_azure_sql_tcp_prefix(self):
        r = adonet.parse(
            "Server=tcp:myserver.database.windows.net,1433;"
            "Initial Catalog=mydb;User Id=user@myserver;Password=pw;"
            "Encrypt=True;TrustServerCertificate=False;"
        )
        assert r.dialect == "mssql"
        assert r.host == "myserver.database.windows.net"
        assert r.port == 1433
        assert r.params.get("encrypt") == "True"

    def test_case_insensitive_keys(self):
        r = adonet.parse("SERVER=host;DATABASE=db;USER ID=usr;PASSWORD=pw;")
        assert r.host == "host"
        assert r.database == "db"
        assert r.user == "usr"

    def test_dialect_inferred_from_port(self):
        r = adonet.parse("Server=host;Port=5432;Database=db;User Id=u;Password=p;")
        assert r.dialect == "postgres"


# ---------------------------------------------------------------------------
# odbc.py
# ---------------------------------------------------------------------------


class TestOdbcParser:
    def test_odbc_driver_17_mssql(self):
        r = odbc.parse(
            "Driver={ODBC Driver 17 for SQL Server};Server=localhost;Database=mydb;"
            "UID=sa;PWD=tiger;"
        )
        assert r.dialect == "mssql"
        assert r.format == "odbc"
        assert r.host == "localhost"
        assert r.database == "mydb"
        assert r.user == "sa"
        assert r.password == "tiger"
        assert r.params.get("driver") == "ODBC Driver 17 for SQL Server"

    def test_odbc_driver_18_mssql(self):
        r = odbc.parse(
            "Driver={ODBC Driver 18 for SQL Server};Server=sqlhost,1433;Database=db;"
            "UID=sa;PWD=pw;Encrypt=yes;"
        )
        assert r.dialect == "mssql"
        assert r.port == 1433

    def test_generic_sql_server_driver(self):
        r = odbc.parse(
            "Driver={SQL Server};Server=host;Database=db;UID=user;PWD=pass;"
        )
        assert r.dialect == "mssql"

    def test_mysql_odbc_driver(self):
        r = odbc.parse(
            "Driver={MySQL ODBC 8.0 Unicode Driver};Server=mysql.host;Database=shop;"
            "UID=root;PWD=rootpw;"
        )
        assert r.dialect == "mysql"
        assert r.host == "mysql.host"

    def test_postgresql_odbc_driver(self):
        r = odbc.parse(
            "Driver={PostgreSQL ODBC Driver(UNICODE)};Server=pghost;Database=mydb;"
            "UID=pguser;PWD=pgpass;"
        )
        assert r.dialect == "postgres"
        assert r.database == "mydb"

    def test_trusted_connection_blocks(self):
        with pytest.raises(UnsupportedConnString):
            odbc.parse(
                "Driver={ODBC Driver 17 for SQL Server};Server=host;Database=db;"
                "Trusted_Connection=yes;"
            )

    def test_driver_name_stored_in_params(self):
        r = odbc.parse(
            "Driver={ODBC Driver 17 for SQL Server};Server=h;Database=d;UID=u;PWD=p;"
        )
        assert "driver" in r.params
        assert "ODBC Driver 17" in r.params["driver"]


# ---------------------------------------------------------------------------
# jdbc.py
# ---------------------------------------------------------------------------


class TestJdbcParser:
    def test_postgresql(self):
        r = jdbc.parse("jdbc:postgresql://localhost:5432/mydb?sslmode=require")
        assert r.format == "jdbc"
        assert r.dialect == "postgres"
        assert r.host == "localhost"
        assert r.port == 5432
        assert r.database == "mydb"
        assert r.params.get("sslmode") == "require"

    def test_mysql(self):
        r = jdbc.parse("jdbc:mysql://localhost:3306/mydb?useSSL=false")
        assert r.dialect == "mysql"
        assert r.port == 3306
        assert r.params.get("useSSL") == "false"

    def test_sqlserver(self):
        r = jdbc.parse(
            "jdbc:sqlserver://localhost:1433;databaseName=mydb;user=sa;password=tiger;"
        )
        assert r.dialect == "mssql"
        assert r.host == "localhost"
        assert r.port == 1433
        assert r.database == "mydb"
        assert r.user == "sa"
        assert r.password == "tiger"

    def test_oracle_service_name(self):
        r = jdbc.parse("jdbc:oracle:thin:@//orahost:1521/ORCL_SERVICE")
        assert r.dialect == "oracle"
        assert r.host == "orahost"
        assert r.port == 1521
        assert r.database == "ORCL_SERVICE"
        assert r.params.get("service_name") == "ORCL_SERVICE"

    def test_oracle_sid(self):
        r = jdbc.parse("jdbc:oracle:thin:@orahost:1521:MYORCL")
        assert r.dialect == "oracle"
        assert r.host == "orahost"
        assert r.port == 1521
        assert r.params.get("sid") == "MYORCL"
        assert r.database == "MYORCL"

    def test_clickhouse(self):
        r = jdbc.parse("jdbc:clickhouse://ch.host:8123/default")
        assert r.dialect == "clickhouse"
        assert r.host == "ch.host"
        assert r.port == 8123
        assert r.database == "default"

    def test_mongodb_srv_delegates_to_cloud(self):
        r = jdbc.parse(
            "jdbc:mongodb+srv://user:pass@cluster0.abc123.mongodb.net/mydb"
        )
        assert r.dialect == "mongodb"
        assert r.params.get("srv") == "true"
        assert r.format == "cloud"

    def test_jdbc_prefix_preserved_in_raw(self):
        raw = "jdbc:postgresql://localhost/db"
        r = jdbc.parse(raw)
        assert r.raw == raw


# ---------------------------------------------------------------------------
# cloud.py
# ---------------------------------------------------------------------------


class TestCloudParser:
    def test_mongodb_srv_with_params(self):
        r = cloud.parse(
            "mongodb+srv://scott:tiger@cluster0.abc123.mongodb.net/mydb"
            "?retryWrites=true&w=majority"
        )
        assert r.format == "cloud"
        assert r.dialect == "mongodb"
        assert r.params.get("srv") == "true"
        assert r.params.get("retryWrites") == "true"
        assert r.params.get("w") == "majority"

    def test_motherduck_with_token(self):
        r = cloud.parse("md:mydb?motherduck_token=tok123")
        assert r.format == "cloud"
        assert r.dialect == "duckdb"
        assert r.database == "md:mydb"
        assert r.params.get("motherduck_token") == "tok123"

    def test_motherduck_without_token(self):
        r = cloud.parse("md:mydb")
        assert r.format == "cloud"
        assert r.dialect == "duckdb"
        assert r.database == "md:mydb"
        assert "motherduck_token" not in r.params

    def test_motherduck_empty_dbname(self):
        r = cloud.parse("md:")
        assert r.dialect == "duckdb"
        assert r.database == "md:"

    def test_clickhouse_cloud_hostname_via_uri(self):
        r = cloud.parse("clickhouse+http://default:token@abc123.clickhouse.cloud/default")
        assert r.format == "cloud"
        assert r.dialect == "clickhouse"
        assert r.params.get("secure") == "true"
        # Port should be 8443 if not specified — here it was not specified in URL
        assert r.port == 8443

    def test_clickhouse_cloud_hostname_port_preserved_if_explicit(self):
        r = cloud.parse("clickhouse+http://user:pass@abc.clickhouse.cloud:9999/db")
        assert r.port == 9999  # explicit port preserved
        assert r.params.get("secure") == "true"

    def test_from_uri_parsed_sets_secure_and_default_port(self):
        from dbread.connstr.types import ParsedConn
        p = ParsedConn(format="uri", dialect="clickhouse", host="x.clickhouse.cloud", raw="")
        result = cloud.from_uri_parsed(p)
        assert result.format == "cloud"
        assert result.port == 8443
        assert result.params.get("secure") == "true"


# ---------------------------------------------------------------------------
# filepath.py
# ---------------------------------------------------------------------------


class TestFilepathParser:
    def test_relative_sqlite_db(self):
        r = filepath.parse("mydata.db")
        assert r.dialect == "sqlite"
        assert r.format == "filepath"
        # database should be absolute path with forward slashes
        assert "/" in r.database
        assert r.database.endswith("mydata.db")

    def test_sqlite3_extension(self):
        r = filepath.parse("archive.sqlite3")
        assert r.dialect == "sqlite"

    def test_duckdb_extension(self):
        r = filepath.parse("analytics.duckdb")
        assert r.dialect == "duckdb"
        assert r.database.endswith("analytics.duckdb")

    def test_memory_default_sqlite(self):
        r = filepath.parse(":memory:")
        assert r.dialect == "sqlite"
        assert r.database == ":memory:"

    def test_memory_with_hint(self):
        r = filepath.parse(":memory:", dialect_hint="duckdb")
        assert r.dialect == "duckdb"
        assert r.database == ":memory:"

    def test_windows_path_forward_slashes(self):
        # Simulate a Windows absolute path — Path.resolve() returns OS-native;
        # as_posix() converts to forward slashes regardless of OS
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db_file = os.path.join(tmp, "test.db")
            r = filepath.parse(db_file)
            assert "\\" not in r.database
            assert "/" in r.database

    def test_unknown_extension_raises(self):
        with pytest.raises(UnknownFormat):
            filepath.parse("archive.csv")

    def test_sqlite_full_extension(self):
        r = filepath.parse("warehouse.sqlite")
        assert r.dialect == "sqlite"
