CREATE TABLE demo_error_control (id INT, enabled BOOLEAN);
INSERT INTO demo_error_control VALUES (1, TRUE);
INSERT INTO demo_error_control VALUES ('bad', FALSE);
INSERT INTO demo_error_control VALUES (2, FALSE);
SELECT * FROM demo_error_control;
