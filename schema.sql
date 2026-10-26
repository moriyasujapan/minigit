-- schema demo
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  name VARCHAR(64) NOT NULL,
  created DATE DEFAULT NULL
);
SELECT name, count(*) AS n FROM users GROUP BY name HAVING count(*) > 1 ORDER BY n DESC; -- top
