"""SQL validation gate: AST-based read-only guard + LIMIT injection.

This is Layer 1 of defense in depth. Layer 0 (read-only DB user) remains
the ultimate guarantee; this layer rejects DML/DDL/DCL before they hit
the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

# MSSQL WAITFOR is a statement, and sqlglot's tsql dialect raises ParseError
# on it. We reject it explicitly with a clearer reason before handing to parse.
_WAITFOR_RE = re.compile(r"\bWAITFOR\s+(?:DELAY|TIME)\b", re.IGNORECASE)

# Strip SQL line + block comments before the top-level scan so commenters
# can't bypass with `/* x */ WAITFOR`.
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

REJECT_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Into,
)

# sqlglot may or may not expose Revoke depending on version; add if present.
_REVOKE = getattr(exp, "Revoke", None)
if _REVOKE is not None:
    REJECT_NODES = REJECT_NODES + (_REVOKE,)

ALLOW_TOP_LEVEL: tuple[type[exp.Expression], ...] = (
    exp.Select,
    exp.Union,
    exp.Describe,
    exp.Show,
    exp.With,
    exp.Use,
)

ALLOW_COMMAND_NAMES = {"EXPLAIN", "DESCRIBE", "DESC", "ANALYZE"}

FUNCTION_BLACKLIST = {
    # PostgreSQL - file/network/admin
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "lo_from_bytea", "lo_put", "lo_unlink",
    "dblink_exec", "dblink_send_query",
    "pg_advisory_lock", "pg_advisory_lock_shared",
    "pg_advisory_xact_lock", "pg_try_advisory_lock",
    "pg_terminate_backend", "pg_cancel_backend",
    "pg_reload_conf", "pg_rotate_logfile",
    # PostgreSQL - time-based DoS
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    # MSSQL - extended procs / config
    "xp_cmdshell", "xp_regread", "xp_regwrite", "xp_dirtree",
    "sp_oacreate", "sp_oamethod", "sp_configure",
    # MySQL - file / time-based
    "load_file", "sleep", "benchmark",
    # Oracle - utilities + sleep (dbms_lock.sleep parses to name='sleep' —
    # already covered by generic 'sleep' above, listed here for intent).
    "dbms_xmlgen", "utl_file", "utl_http",
    # ClickHouse - external readers / table functions that pierce the DB
    # boundary (fetch from remote hosts, cloud storage, other DBs).
    "url", "s3", "hdfs", "remote", "remote_secure",
    "mysql_table", "postgresql_table", "mongodb",
    # DuckDB - file readers (powerful analytics features, but also Layer-1
    # bypass of the DB-user filesystem isolation). Two forms below:
    #   - lowercase string: matches typed AST class names (readcsv, readparquet)
    #     and anonymous function calls.
    #   - the `read_*` aliases users type get parsed into those classes.
    "read_csv", "read_csv_auto", "read_parquet",
    "read_json", "read_json_auto", "read_ndjson",
    "readcsv", "readparquet",  # matches class names of typed AST nodes
}


@dataclass
class GuardResult:
    allowed: bool
    reason: str | None = None
    ast: exp.Expression | None = None


class SqlGuard:
    def validate(self, sql: str, dialect: str) -> GuardResult:
        if not sql or not sql.strip():
            return GuardResult(False, "empty_sql")

        stripped = _BLOCK_COMMENT_RE.sub(" ", _LINE_COMMENT_RE.sub(" ", sql))
        if _WAITFOR_RE.search(stripped):
            return GuardResult(False, "command_rejected: WAITFOR")

        try:
            stmts = sqlglot.parse(sql, read=dialect)
        except sqlglot.errors.ParseError as e:
            return GuardResult(False, f"parse_error: {e}")

        stmts = [s for s in stmts if s is not None]
        if len(stmts) == 0:
            return GuardResult(False, "empty_sql")
        if len(stmts) > 1:
            return GuardResult(False, "multi_statement_not_allowed")

        root = stmts[0]

        if not self._top_level_allowed(root):
            return GuardResult(False, f"top_level_not_allowed: {type(root).__name__}")

        for node in root.walk():
            if isinstance(node, REJECT_NODES):
                return GuardResult(False, f"node_rejected: {type(node).__name__}")
            if isinstance(node, exp.Command) and node is not root:
                return GuardResult(
                    False, f"nested_command_rejected: {node.name}"
                )
            # sqlglot recognizes some dangerous functions as typed AST nodes
            # (e.g. ReadCSV, ReadParquet) — match on class name so the
            # blacklist covers both anonymous calls and typed nodes.
            cls_name = type(node).__name__.lower()
            if cls_name in FUNCTION_BLACKLIST:
                return GuardResult(False, f"function_blacklisted: {cls_name}")
            if isinstance(node, (exp.Anonymous, exp.Func)):
                name = (node.name or "").lower()
                if name in FUNCTION_BLACKLIST:
                    return GuardResult(False, f"function_blacklisted: {name}")

        return GuardResult(True, ast=root)

    def _top_level_allowed(self, root: exp.Expression) -> bool:
        if isinstance(root, ALLOW_TOP_LEVEL):
            return True
        if isinstance(root, exp.Command):
            return (root.name or "").upper() in ALLOW_COMMAND_NAMES
        return False

    def inject_limit(self, sql: str, dialect: str, max_rows: int) -> str:
        try:
            stmts = sqlglot.parse(sql, read=dialect)
        except sqlglot.errors.ParseError:
            return sql
        if len(stmts) != 1 or stmts[0] is None:
            return sql
        root = stmts[0]
        self._apply_limit(root, max_rows)
        return root.sql(dialect=dialect)

    def _apply_limit(self, root: exp.Expression, max_rows: int) -> None:
        if isinstance(root, exp.Select | exp.Union) and not root.args.get("limit"):
            root.limit(max_rows, copy=False)
        elif isinstance(root, exp.With):
            inner = root.this
            if isinstance(inner, exp.Select) and not inner.args.get("limit"):
                inner.limit(max_rows, copy=False)
