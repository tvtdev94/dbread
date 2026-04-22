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

INSERT INTO users(name) VALUES ('alice'), ('bob'), ('carol');
INSERT INTO orders(user_id, total) VALUES (1, 100), (1, 200), (2, 50);

GRANT SELECT ON ALL TABLES IN SCHEMA public TO ai_readonly;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO ai_readonly;

ALTER USER ai_readonly SET default_transaction_read_only = on;
ALTER USER ai_readonly SET statement_timeout = '5s';
