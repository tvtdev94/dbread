# DB Read-Only Setup (Layer 0)

> **Non-negotiable security foundation.** The parser guard + rate limit are belts; the DB user is the suspenders.

## Why This Is Mandatory

- Layer 1 (sqlglot guard) can have bugs or edge cases. Layer 0 (a DB user with no write permission) is the last line of defense that cannot be bypassed by any SQL trick.
- **Configure this BEFORE exposing a connection to dbread.** Do not point dbread at an admin user.

---

## PostgreSQL

### 1. Create user

```sql
CREATE USER ai_readonly WITH PASSWORD 'CHANGEME_strong_password';
```

### 2. Grant read-only on database + schema

```sql
-- Connect to target DB first (\c mydb in psql)
GRANT CONNECT ON DATABASE mydb TO ai_readonly;
GRANT USAGE ON SCHEMA public TO ai_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ai_readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO ai_readonly;

-- Future tables: auto-grant SELECT
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO ai_readonly;
```

### 3. Enforce read-only transaction + timeout

```sql
ALTER USER ai_readonly SET default_transaction_read_only = on;
ALTER USER ai_readonly SET statement_timeout = '30s';
ALTER USER ai_readonly SET idle_in_transaction_session_timeout = '60s';
```

### 4. Verify (log in as ai_readonly)

```sql
SELECT 1;                              -- should succeed
CREATE TABLE test(id int);             -- should FAIL: permission denied
UPDATE any_table SET x = 1;            -- should FAIL: read-only transaction
```

### 5. Connection string (for `config.yaml`)

```
postgresql+psycopg2://ai_readonly:strong_password@host:5432/mydb
```

---

## MySQL 8+

### 1. Create user

```sql
CREATE USER 'ai_readonly'@'%' IDENTIFIED BY 'CHANGEME_strong_password';
```

### 2. Grant

```sql
GRANT SELECT, SHOW VIEW ON mydb.* TO 'ai_readonly'@'%';
FLUSH PRIVILEGES;
```

### 3. Timeout

```sql
-- Global (requires SUPER):
SET GLOBAL MAX_EXECUTION_TIME = 30000;   -- milliseconds
```

dbread additionally sets `SET SESSION MAX_EXECUTION_TIME` per connection via `init_command`.

### 4. Verify

```sql
SELECT 1;                  -- OK
CREATE TABLE t(id int);    -- FAIL: command denied
UPDATE any_table ...;      -- FAIL
```

### 5. Connection string

```
mysql+pymysql://ai_readonly:strong_password@host:3306/mydb
```

---

## Microsoft SQL Server

### 1. Create login + user

```sql
CREATE LOGIN ai_readonly WITH PASSWORD = 'CHANGEME_Strong_Pw!';
USE mydb;
CREATE USER ai_readonly FOR LOGIN ai_readonly;
```

### 2. Grant read-only role + deny writes and execute

```sql
ALTER ROLE db_datareader ADD MEMBER ai_readonly;
DENY EXECUTE TO ai_readonly;                   -- blocks xp_cmdshell & all stored procs
DENY ALTER, INSERT, UPDATE, DELETE TO ai_readonly;
```

### 3. Query timeout

MSSQL has no native per-user query timeout. Options:

- dbread passes pyodbc `timeout` arg (connection-level).
- Server-wide Query Governor:

```sql
sp_configure 'query governor cost limit', 30;   -- rough cost-based limit
RECONFIGURE;
```

### 4. Verify

```sql
SELECT 1;                           -- OK
UPDATE dbo.any_table SET x = 1;     -- FAIL
EXEC xp_cmdshell 'dir';             -- FAIL (DENY EXECUTE)
```

### 5. Connection string (ODBC Driver 18)

```
mssql+pyodbc://ai_readonly:Strong_Pw!@host/mydb?driver=ODBC+Driver+18+for+SQL+Server
```

---

## Oracle

### 1. Create user + grants

```sql
CREATE USER ai_readonly IDENTIFIED BY "CHANGEME_strong_pw";
GRANT CREATE SESSION TO ai_readonly;
GRANT SELECT ANY TABLE TO ai_readonly;
-- Tighter option: per-table GRANT SELECT ON schema.tbl TO ai_readonly;
-- Do NOT grant CREATE / ALTER / DROP privileges.
```

### 2. Resource profile (timeout + idle)

```sql
CREATE PROFILE readonly_profile LIMIT
  IDLE_TIME     5                -- minutes
  CONNECT_TIME  60
  CPU_PER_CALL  3000;            -- centiseconds = 30s
ALTER USER ai_readonly PROFILE readonly_profile;
```

### 3. Verify

```sql
SELECT 1 FROM DUAL;              -- OK
CREATE TABLE t(x NUMBER);        -- FAIL: insufficient privileges
```

### 4. Connection string

```
oracle+oracledb://ai_readonly:strong_pw@host:1521/?service_name=mydb
```

---

## SQLite

SQLite has no user system. Enforce read-only via file permissions + URI mode.

### 1. File permission (Unix)

```bash
chmod 444 mydb.sqlite          # strict read-only at FS level
```

On Windows, set file attributes to read-only via File Explorer → Properties, or:

```powershell
attrib +r mydb.sqlite
```

### 2. Connection string (URI mode, read-only)

```
sqlite:///file:mydb.sqlite?mode=ro&uri=true
```

Any `INSERT/UPDATE/DELETE/CREATE` will fail with `attempt to write a readonly database`.

---

## Verification Checklist (all DBs)

- [ ] `SELECT 1` works
- [ ] `CREATE / ALTER / DROP` fail with permission error
- [ ] `INSERT / UPDATE / DELETE` fail
- [ ] Long query (`SELECT pg_sleep(60)` etc) times out at the configured threshold
- [ ] Side-effect functions where applicable (`pg_read_file`, `xp_cmdshell`) → permission denied
- [ ] The credential is in `.env` (referenced via `url_env`) — never hardcoded in `config.yaml`
