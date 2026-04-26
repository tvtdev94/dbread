# Connection-string formats supported by `dbread add`

`dbread add` auto-detects and converts these connection-string formats into the SQLAlchemy URL that dbread uses
internally. You don't need to do any conversion yourself — paste any of the formats below and dbread handles the rest.

## Quick reference

| Format        | Detection                                  | Example (postgres)                                          |
|---------------|--------------------------------------------|-------------------------------------------------------------|
| Native URI    | starts with `<dialect>://`                 | `postgresql://user:pw@host:5432/db?sslmode=require`         |
| SQLAlchemy    | starts with `<dialect>+driver://`          | `postgresql+psycopg2://user:pw@host:5432/db`                |
| JDBC URL      | starts with `jdbc:`                        | `jdbc:postgresql://host:5432/db?user=u&password=p`          |
| ADO.NET / C#  | `key=value;` with `Server=` or `Host=`     | `Server=host;Database=db;User Id=u;Password=p;`             |
| ODBC          | `Driver={...};...`                         | `Driver={ODBC Driver 17 for SQL Server};Server=...;`        |
| MongoDB Atlas | starts with `mongodb+srv://`               | `mongodb+srv://u:p@cluster0.abc.mongodb.net/db`             |
| MotherDuck    | starts with `md:`                          | `md:mydb?motherduck_token=xxx`                              |
| File path     | ends in `.db` / `.sqlite` / `.duckdb`      | `~/data/analytics.duckdb`                                   |

## Per-dialect examples

### PostgreSQL

1. **Native URI** (recommended):
   ```
   postgresql://user:password@host:5432/database
   ```
   Supports query params: `sslmode=require`, `options=...`, etc.

2. **JDBC URL**:
   ```
   jdbc:postgresql://host:5432/database?user=user&password=pwd
   ```

3. **ADO.NET / C#**:
   ```
   Server=host;Port=5432;Database=db;User Id=user;Password=pwd;
   ```

### MySQL

1. **Native URI** (recommended):
   ```
   mysql+pymysql://user:password@host:3306/database
   ```

2. **JDBC URL**:
   ```
   jdbc:mysql://host:3306/database?user=user&password=pwd
   ```

3. **ADO.NET / C#**:
   ```
   Server=host;Port=3306;Database=db;Uid=user;Pwd=pwd;
   ```

### MSSQL

1. **Native URI** (recommended):
   ```
   mssql+pyodbc://user:password@host:1433/database?driver=ODBC+Driver+17+for+SQL+Server
   ```

2. **ODBC**:
   ```
   Driver={ODBC Driver 17 for SQL Server};Server=host;Database=db;Uid=user;Pwd=pwd;
   ```

3. **JDBC URL**:
   ```
   jdbc:sqlserver://host:1433;database=db;user=user;password=pwd;
   ```

### Oracle

1. **Native URI** (recommended):
   ```
   oracle+cx_oracle://user:password@host:1521/SERVICENAME
   ```

2. **EZ Connect** (better than TNS descriptor):
   ```
   oracle://user:password@host:1521/service_name
   ```

### SQLite

1. **File path** (simplest):
   ```
   ~/data/analytics.sqlite
   ```
   Also recognized: `.db`, `.sqlite3`.

2. **Native URI**:
   ```
   sqlite:///path/to/file.db
   ```

### DuckDB

1. **File path** (simplest):
   ```
   ~/data/analytics.duckdb
   ```

2. **Native URI**:
   ```
   duckdb:///path/to/file.duckdb?access_mode=read_only
   ```

### ClickHouse

1. **Native URI** (recommended):
   ```
   clickhouse+http://user:password@host:8123/database
   ```

### MongoDB

1. **MongoDB Atlas** (cloud, simplest):
   ```
   mongodb+srv://user:password@cluster0.abc.mongodb.net/database?tls=true
   ```

2. **Self-hosted**:
   ```
   mongodb://user:password@host:27017/database
   ```

### MotherDuck (DuckDB Cloud)

1. **MotherDuck URI**:
   ```
   md:mydb?motherduck_token=eyJ0eXAiOiJKV1QiLCJhbGc...
   ```

## Default ports applied when missing

When you omit a port in your connection string, dbread uses these defaults:

| Dialect    | Default Port |
|------------|--------------|
| PostgreSQL | 5432         |
| MySQL      | 3306         |
| MSSQL      | 1433         |
| Oracle     | 1521         |
| ClickHouse | 8123         |
| MongoDB    | 27017        |

## Special characters in passwords

dbread URL-escapes passwords automatically. You can paste passwords containing `@` `:` `/` `+` `%` `#` `?` `&` `=` — no need to encode yourself.

## Cannot be auto-converted

These formats need manual setup. `dbread add` blocks them with a hint:

| Input | Why | What to do |
|-------|-----|------------|
| `Trusted_Connection=yes` (MSSQL Windows auth) | dbread needs explicit user/password | Provide a SQL login user, or set up Kerberos manually in your URL |
| Oracle TNS descriptor `(DESCRIPTION=...)` | Complex nested parser | Use EZ Connect: `host:port/service_name` |
| MSSQL named instance `HOST\SQLEXPRESS` | Requires ODBC DSN | Use `IP:port` or pre-configure an ODBC DSN |

## Reference

For the complete format spec used by the parser (regex patterns, conversion rules, edge cases), see the
implementation in the dbread repository under `src/dbread/connstr/`.

## When auto-detect fails

If `dbread add` cannot recognise your connection string, you have 3 options.

### Option 1 — Manual SQLAlchemy URL (recommended)
Skip detection entirely:

```bash
dbread add prod_pg --manual --dialect-hint postgres
```

You'll be prompted for the SQLAlchemy URL directly. Templates per dialect:

| Dialect    | SQLAlchemy URL template                                       |
|------------|---------------------------------------------------------------|
| postgres   | `postgresql+psycopg2://user:pw@host:5432/db`                  |
| mysql      | `mysql+pymysql://user:pw@host:3306/db`                        |
| mssql      | `mssql+pyodbc://user:pw@host:1433/db?driver=ODBC+Driver+17+for+SQL+Server` |
| oracle     | `oracle+oracledb://user:pw@host:1521/?service_name=XE`        |
| sqlite     | `sqlite:///path/to/file.db` (or `sqlite:///:memory:`)         |
| duckdb     | `duckdb:///path/to/file.duckdb` (or `duckdb:///md:dbname?motherduck_token=...`) |
| clickhouse | `clickhouse+http://user:pw@host:8123/db`                      |
| mongodb    | `mongodb://user:pw@host:27017/db` (or `mongodb+srv://...`)    |

URL-escape passwords containing `@ : / + % # ? & =` using `urllib.parse.quote`.

### Option 2 — Force dialect on a string the parser misclassifies
```bash
dbread add --dialect-hint mssql
```

### Option 3 — Edit `~/.dbread/config.yaml` directly
Paste this template and adjust:

```yaml
connections:
  prod_pg:
    url_env: PROD_PG_URL
    dialect: postgres
    rate_limit_per_min: 60
    statement_timeout_s: 30
    max_rows: 1000
```

Then add the URL to `~/.dbread/.env`:
```
PROD_PG_URL=postgresql+psycopg2://user:pw@host:5432/db
```

Verify with `dbread doctor`.
