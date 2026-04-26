"""Unit tests for the connection-string format detector / dispatcher."""

from __future__ import annotations

import pytest

from dbread.connstr.detector import detect_and_parse
from dbread.connstr.types import UnknownFormat, UnsupportedConnString


class TestJdbcPriority:
    def test_jdbc_prefix_wins_over_uri(self):
        """jdbc:postgresql://... must be routed to jdbc parser, not uri parser."""
        r = detect_and_parse("jdbc:postgresql://user:pass@localhost:5432/db")
        assert r.format == "jdbc"
        assert r.dialect == "postgres"

    def test_jdbc_sqlserver(self):
        r = detect_and_parse(
            "jdbc:sqlserver://host:1433;databaseName=db;user=sa;password=pw;"
        )
        assert r.format == "jdbc"
        assert r.dialect == "mssql"

    def test_jdbc_oracle_service_name(self):
        r = detect_and_parse("jdbc:oracle:thin:@//orahost:1521/MYSERVICE")
        assert r.format == "jdbc"
        assert r.dialect == "oracle"

    def test_jdbc_mysql(self):
        r = detect_and_parse("jdbc:mysql://localhost:3306/mydb")
        assert r.format == "jdbc"
        assert r.dialect == "mysql"

    def test_jdbc_mongodb_srv_routes_to_cloud(self):
        r = detect_and_parse(
            "jdbc:mongodb+srv://user:pass@cluster.mongodb.net/mydb"
        )
        assert r.format == "cloud"
        assert r.dialect == "mongodb"


class TestCloudMarkers:
    def test_mongodb_srv_routes_to_cloud(self):
        r = detect_and_parse(
            "mongodb+srv://user:pass@cluster0.abc123.mongodb.net/mydb?retryWrites=true"
        )
        assert r.format == "cloud"
        assert r.dialect == "mongodb"
        assert r.params.get("srv") == "true"

    def test_md_prefix_routes_to_cloud(self):
        r = detect_and_parse("md:mydb?motherduck_token=tok123")
        assert r.format == "cloud"
        assert r.dialect == "duckdb"

    def test_md_no_token(self):
        r = detect_and_parse("md:analytics")
        assert r.format == "cloud"
        assert r.dialect == "duckdb"
        assert r.database == "md:analytics"


class TestUriRouting:
    def test_postgresql_uri_routes_to_uri(self):
        r = detect_and_parse("postgresql://user:pass@localhost/db")
        assert r.format == "uri"
        assert r.dialect == "postgres"

    def test_mysql_uri(self):
        r = detect_and_parse("mysql://root:pw@host:3306/shop")
        assert r.format == "uri"
        assert r.dialect == "mysql"

    def test_clickhouse_cloud_hostname_rerouted_to_cloud(self):
        """URI with *.clickhouse.cloud host must be re-routed to cloud parser."""
        r = detect_and_parse(
            "clickhouse+http://default:token@abc123.clickhouse.cloud/default"
        )
        assert r.format == "cloud"
        assert r.dialect == "clickhouse"
        assert r.params.get("secure") == "true"

    def test_regular_clickhouse_uri_stays_uri(self):
        r = detect_and_parse("clickhouse+http://user:pw@localhost:8123/default")
        assert r.format == "uri"
        assert r.dialect == "clickhouse"


class TestOdbcDetection:
    def test_odbc_driver_curly_braces(self):
        r = detect_and_parse(
            "Driver={ODBC Driver 17 for SQL Server};Server=host;Database=db;"
            "UID=user;PWD=pw;"
        )
        assert r.format == "odbc"
        assert r.dialect == "mssql"

    def test_odbc_mysql_driver(self):
        r = detect_and_parse(
            "Driver={MySQL ODBC 8.0 Unicode Driver};Server=host;Database=db;"
            "UID=root;PWD=pw;"
        )
        assert r.format == "odbc"
        assert r.dialect == "mysql"

    def test_odbc_case_insensitive_driver_key(self):
        r = detect_and_parse(
            "driver={SQL Server};Server=host;Database=db;UID=user;PWD=pw;"
        )
        assert r.format == "odbc"


class TestAdonetDetection:
    def test_adonet_server_key(self):
        r = detect_and_parse(
            "Server=localhost;Database=mydb;User Id=sa;Password=pw;"
        )
        assert r.format == "adonet"

    def test_adonet_data_source_key(self):
        r = detect_and_parse(
            "Data Source=localhost:1521/ORCL;User Id=scott;Password=tiger;"
        )
        assert r.format == "adonet"

    def test_adonet_host_key(self):
        r = detect_and_parse("Host=pghost;Port=5432;Database=db;User Id=u;Password=p;")
        assert r.format == "adonet"

    def test_adonet_trusted_connection_raises(self):
        with pytest.raises(UnsupportedConnString):
            detect_and_parse(
                "Server=host;Database=db;Trusted_Connection=yes;"
            )


class TestFilepathDetection:
    def test_sqlite_db_extension(self):
        r = detect_and_parse("myapp.db")
        assert r.format == "filepath"
        assert r.dialect == "sqlite"

    def test_duckdb_extension(self):
        r = detect_and_parse("warehouse.duckdb")
        assert r.format == "filepath"
        assert r.dialect == "duckdb"

    def test_memory_shorthand(self):
        r = detect_and_parse(":memory:")
        assert r.format == "filepath"
        assert r.dialect == "sqlite"

    def test_sqlite3_extension(self):
        r = detect_and_parse("archive.sqlite3")
        assert r.format == "filepath"


class TestEdgeCases:
    def test_unknown_string_raises(self):
        with pytest.raises(UnknownFormat):
            detect_and_parse("this_is_not_a_connection_string")

    def test_random_words_raise(self):
        with pytest.raises(UnknownFormat):
            detect_and_parse("hello world")

    def test_bom_stripped(self):
        """BOM prefix (U+FEFF) must not prevent correct detection."""
        raw = "﻿postgresql://user:pass@localhost/db"
        r = detect_and_parse(raw)
        assert r.dialect == "postgres"
        assert r.format == "uri"

    def test_leading_whitespace_stripped(self):
        r = detect_and_parse("   postgresql://user:pass@localhost/db   ")
        assert r.dialect == "postgres"

    def test_dialect_hint_passed_through_to_uri(self):
        r = detect_and_parse("postgresql://user:pass@localhost/db", dialect_hint="postgres")
        assert r.dialect == "postgres"

    def test_jdbc_case_insensitive_prefix(self):
        """JDBC prefix check is case-insensitive."""
        r = detect_and_parse("JDBC:postgresql://localhost/db")
        assert r.format == "jdbc"

    def test_empty_string_raises(self):
        with pytest.raises(UnknownFormat):
            detect_and_parse("")

    def test_oracle_named_instance_blocked_via_adonet(self):
        with pytest.raises(UnsupportedConnString):
            detect_and_parse(r"Server=HOST\SQLEXPRESS;Database=db;User Id=sa;Password=pw;")
