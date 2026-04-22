-- Seed table for dbread ClickHouse E2E tests.
-- The server-side readonly=1 profile + connect_args belt-and-braces ensures
-- the dbread user can only SELECT.

CREATE TABLE IF NOT EXISTS testdb.events (
    id UInt32,
    name String,
    ts DateTime DEFAULT now()
) ENGINE = MergeTree ORDER BY id;

INSERT INTO testdb.events (id, name) VALUES (1, 'alpha'), (2, 'beta'), (3, 'gamma');
