CREATE USER 'ai_readonly'@'%' IDENTIFIED BY 'ropw';
GRANT SELECT, SHOW VIEW ON testdb.* TO 'ai_readonly'@'%';
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
