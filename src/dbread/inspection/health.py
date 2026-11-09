"""Server health sections for `db_health`, one read-only SELECT each.

Each section is independent: a missing extension or privilege fails only
that section, and the hint names the exact grant or extension that fixes it.
Every statement must pass `SqlGuard.validate` for its dialect, since it runs
through the normal query path.
"""

from __future__ import annotations

from dataclasses import dataclass

SECTION_ROWS = 50
# Statement text is cut server-side: other sessions' SQL can be huge and
# carries their literals (see docs/security-threat-model.md).
QUERY_CHARS = 200


@dataclass(frozen=True)
class Section:
    sql: str
    hint: str


_PG_STATS_HINT = (
    "GRANT pg_read_all_stats TO <dbread_user>; shows every session and "
    "statistic, not only the user's own"
)

PG_SECTIONS: dict[str, Section] = {
    "top_statements": Section(
        "SELECT queryid, calls, total_exec_time AS total_ms, "
        f"mean_exec_time AS mean_ms, rows AS rows_returned, LEFT(query, {QUERY_CHARS}) AS query "
        "FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 20",
        "CREATE EXTENSION pg_stat_statements; (needs "
        "shared_preload_libraries = 'pg_stat_statements' and a restart), then "
        + _PG_STATS_HINT,
    ),
    "unused_indexes": Section(
        "SELECT s.schemaname AS schema_name, s.relname AS table_name, "
        "s.indexrelname AS index_name, s.idx_scan, "
        "pg_relation_size(s.indexrelid) AS size_bytes "
        "FROM pg_stat_user_indexes AS s "
        "JOIN pg_index AS i ON i.indexrelid = s.indexrelid "
        "WHERE s.idx_scan = 0 AND NOT i.indisunique AND NOT i.indisprimary "
        f"ORDER BY pg_relation_size(s.indexrelid) DESC LIMIT {SECTION_ROWS}",
        _PG_STATS_HINT,
    ),
    "dead_tuples": Section(
        "SELECT schemaname AS schema_name, relname AS table_name, n_live_tup, "
        "n_dead_tup, last_vacuum, last_autovacuum "
        "FROM pg_stat_user_tables WHERE n_dead_tup > 0 "
        f"ORDER BY n_dead_tup DESC LIMIT {SECTION_ROWS}",
        _PG_STATS_HINT,
    ),
    "lock_waits": Section(
        "SELECT pid, pg_blocking_pids(pid) AS blocked_by, wait_event_type, "
        "wait_event, state, now() - query_start AS waiting_for, "
        f"LEFT(query, {QUERY_CHARS}) AS query FROM pg_stat_activity "
        "WHERE CARDINALITY(pg_blocking_pids(pid)) > 0 "
        f"ORDER BY query_start LIMIT {SECTION_ROWS}",
        _PG_STATS_HINT,
    ),
    "long_running": Section(
        "SELECT pid, usename, state, now() - xact_start AS transaction_age, "
        f"now() - query_start AS query_age, LEFT(query, {QUERY_CHARS}) AS query "
        "FROM pg_stat_activity WHERE state <> 'idle' "
        "AND xact_start IS NOT NULL AND pid <> pg_backend_pid() "
        f"ORDER BY xact_start LIMIT {SECTION_ROWS}",
        _PG_STATS_HINT,
    ),
}

_MYSQL_SYS_HINT = (
    "GRANT SELECT ON sys.* TO <dbread_user>; GRANT SELECT ON "
    "performance_schema.* TO <dbread_user>; GRANT PROCESS ON *.* TO <dbread_user>"
)

MYSQL_SECTIONS: dict[str, Section] = {
    "top_statements": Section(
        f"SELECT LEFT(DIGEST_TEXT, {QUERY_CHARS}) AS query, COUNT_STAR AS calls, "
        "SUM_TIMER_WAIT / 1000000000 AS total_ms, "
        "AVG_TIMER_WAIT / 1000000000 AS mean_ms, "
        "SUM_ROWS_EXAMINED AS rows_examined "
        "FROM performance_schema.events_statements_summary_by_digest "
        "ORDER BY SUM_TIMER_WAIT DESC LIMIT 20",
        "GRANT SELECT ON performance_schema.* TO <dbread_user>; and keep "
        "performance_schema=ON (the default)",
    ),
    "unused_indexes": Section(
        "SELECT object_schema, object_name AS table_name, index_name "
        f"FROM sys.schema_unused_indexes LIMIT {SECTION_ROWS}",
        _MYSQL_SYS_HINT,
    ),
    "lock_waits": Section(
        "SELECT wait_age, locked_table, waiting_pid, "
        f"LEFT(waiting_query, {QUERY_CHARS}) AS waiting_query, blocking_pid, "
        f"LEFT(blocking_query, {QUERY_CHARS}) AS blocking_query "
        f"FROM sys.innodb_lock_waits LIMIT {SECTION_ROWS}",
        _MYSQL_SYS_HINT,
    ),
    "fragmentation": Section(
        "SELECT TABLE_NAME AS table_name, DATA_LENGTH AS data_bytes, "
        "INDEX_LENGTH AS index_bytes, DATA_FREE AS free_bytes "
        "FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND DATA_FREE > 0 "
        f"ORDER BY DATA_FREE DESC LIMIT {SECTION_ROWS}",
        "information_schema.TABLES lists only tables the user can SELECT from; "
        "GRANT SELECT ON <database>.* TO <dbread_user>",
    ),
}

SECTIONS: dict[str, dict[str, Section]] = {
    "postgres": PG_SECTIONS,
    "mysql": MYSQL_SECTIONS,
}
