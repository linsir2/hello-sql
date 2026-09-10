CREATE TABLE demo_users (id INT, name TEXT, active BOOLEAN);
CREATE TABLE demo_orders (id INT, user_id INT, paid BOOLEAN);
CREATE TABLE demo_items (id INT, order_id INT, sku TEXT, price REAL, active BOOLEAN);

INSERT INTO demo_users VALUES (1, 'alice', TRUE);
INSERT INTO demo_users VALUES (2, 'bob', TRUE);
INSERT INTO demo_users VALUES (3, 'carol', FALSE);

INSERT INTO demo_orders VALUES (10, 1, TRUE);
INSERT INTO demo_orders VALUES (11, 1, FALSE);
INSERT INTO demo_orders VALUES (12, 2, TRUE);
INSERT INTO demo_orders VALUES (13, 99, TRUE);

INSERT INTO demo_items VALUES (100, 10, 'book', 35.5, TRUE);
INSERT INTO demo_items VALUES (101, 10, 'pen', 5, FALSE);
INSERT INTO demo_items VALUES (102, 11, 'lamp', 88, TRUE);
INSERT INTO demo_items VALUES (103, 12, 'keyboard', 199, TRUE);
INSERT INTO demo_items VALUES (104, 77, 'orphan', 1, TRUE);

SELECT u.name, o.id
FROM demo_users u
JOIN demo_orders o ON u.id = o.user_id
WHERE u.active AND o.paid;

SELECT u.name, o.id, i.sku, i.price
FROM demo_users AS u
INNER JOIN demo_orders AS o ON u.id = o.user_id
JOIN demo_items AS i ON o.id = i.order_id AND i.active
WHERE u.active AND o.paid;
