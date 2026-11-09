CREATE USER 'ai_readonly'@'%' IDENTIFIED BY 'ropw';
GRANT SELECT, SHOW VIEW ON testdb.* TO 'ai_readonly'@'%';
-- db_health top_statements reads performance_schema digests. sys.* is
-- deliberately not granted so the privilege-hint path is exercised.
GRANT SELECT ON performance_schema.* TO 'ai_readonly'@'%';
FLUSH PRIVILEGES;

USE testdb;

CREATE TABLE users (
  id INT AUTO_INCREMENT PRIMARY KEY,
  name VARCHAR(64) NOT NULL
);

CREATE TABLE orders (
  id INT AUTO_INCREMENT PRIMARY KEY,
  user_id INT,
  total DECIMAL(10, 2),
  FOREIGN KEY (user_id) REFERENCES users(id)
);

INSERT INTO users(name) VALUES ('alice'), ('bob'), ('carol');
INSERT INTO orders(user_id, total) VALUES (1, 100), (1, 200), (2, 50);

-- Whole-database inspection fixtures: table/column comments, CHECK and
-- UNIQUE constraints, an all-NULL column, a constant column, and an orphan
-- row slipped past the foreign key with FOREIGN_KEY_CHECKS off.
CREATE TABLE products (
  id INT AUTO_INCREMENT PRIMARY KEY,
  sku VARCHAR(32) NOT NULL COMMENT 'Stock keeping unit',
  price DECIMAL(10, 2),
  currency VARCHAR(3),
  discontinued_reason VARCHAR(64),
  CONSTRAINT products_sku_key UNIQUE (sku),
  CONSTRAINT products_price_positive CHECK (price > 0)
) COMMENT = 'Catalogue items';
INSERT INTO products(sku, price, currency)
  VALUES ('A-1', 10, 'USD'), ('A-2', 20, 'USD'), ('A-3', 30, 'USD');

CREATE TABLE shipments (
  id INT AUTO_INCREMENT PRIMARY KEY,
  order_id INT,
  tracking VARCHAR(16),
  CONSTRAINT shipments_order_fk FOREIGN KEY (order_id) REFERENCES orders(id)
);
SET FOREIGN_KEY_CHECKS = 0;
INSERT INTO shipments(order_id, tracking)
  VALUES (1, 'T-1'), (999, 'T-2'), (NULL, 'T-3'), (2, 'T-1');
SET FOREIGN_KEY_CHECKS = 1;

ANALYZE TABLE users, orders, products, shipments;
