-- Runs as POSTGRES_USER (superuser) via docker-entrypoint-initdb.d

CREATE USER ai_readonly WITH PASSWORD 'ropw';
GRANT CONNECT ON DATABASE testdb TO ai_readonly;

\c testdb

GRANT USAGE ON SCHEMA public TO ai_readonly;

CREATE TABLE users (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  user_id INT REFERENCES users(id),
  total NUMERIC(10, 2)
);

-- Column types that break naive aggregate profiling: PostgreSQL has no
-- min(boolean) and no equality operator for json, so both must be excluded
-- from MIN/MAX and COUNT(DISTINCT) respectively.
CREATE TABLE profiles (
  id SERIAL PRIMARY KEY,
  is_active BOOLEAN,
  payload JSON,
  note TEXT
);
INSERT INTO profiles(is_active, payload, note)
  VALUES (true, '{"a":1}', 'x'), (false, '{"b":2}', NULL);

INSERT INTO users(name) VALUES ('alice'), ('bob'), ('carol');
INSERT INTO orders(user_id, total) VALUES (1, 100), (1, 200), (2, 50);

-- Whole-database inspection fixtures: table/column comments, CHECK and
-- UNIQUE constraints, an index nothing scans, an all-NULL column, a constant
-- column, and a NOT VALID foreign key that lets an orphan row in.
CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  sku TEXT NOT NULL CONSTRAINT products_sku_key UNIQUE,
  price NUMERIC(10, 2) CONSTRAINT products_price_positive CHECK (price > 0),
  currency TEXT,
  discontinued_reason TEXT
);
COMMENT ON TABLE products IS 'Catalogue items';
COMMENT ON COLUMN products.sku IS 'Stock keeping unit';
CREATE INDEX idx_products_currency ON products(currency);
INSERT INTO products(sku, price, currency)
  VALUES ('A-1', 10, 'USD'), ('A-2', 20, 'USD'), ('A-3', 30, 'USD');

CREATE TABLE shipments (
  id SERIAL PRIMARY KEY,
  order_id INT,
  tracking TEXT
);
INSERT INTO shipments(order_id, tracking)
  VALUES (1, 'T-1'), (999, 'T-2'), (NULL, 'T-3'), (2, 'T-1');
ALTER TABLE shipments ADD CONSTRAINT shipments_order_fk
  FOREIGN KEY (order_id) REFERENCES orders(id) NOT VALID;

-- db_health: pg_stat_statements needs shared_preload_libraries (set in
-- docker-compose.yml); pg_read_all_stats lets a non-superuser see it all.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
GRANT pg_read_all_stats TO ai_readonly;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO ai_readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO ai_readonly;

ALTER USER ai_readonly SET default_transaction_read_only = on;
ALTER USER ai_readonly SET statement_timeout = '5s';

-- Fresh tables report reltuples = -1 until analyzed.
ANALYZE;
