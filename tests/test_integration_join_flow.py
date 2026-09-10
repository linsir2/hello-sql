"""基于真实编译器、Runner 和页式存储的 INNER JOIN 综合测试。"""

from __future__ import annotations

from collections import Counter

from compiler import parse
from runner import Runner
from storage import DatabaseServer


def _runner(tmp_path) -> Runner:
    return Runner(DatabaseServer(tmp_path / "data"), parse)


def _execute_all(runner: Runner, statements: tuple[str, ...]) -> None:
    for statement in statements:
        runner.execute(statement)


def test_single_join_executes_through_real_storage(tmp_path) -> None:
    """单 JOIN 覆盖一对多、无匹配行、别名、BOOLEAN 过滤与限定投影。"""
    runner = _runner(tmp_path)
    _execute_all(
        runner,
        (
            "CREATE TABLE users (id INT, name TEXT, active BOOLEAN);",
            "CREATE TABLE orders (id INT, user_id INT, total REAL, cancelled BOOLEAN);",
            "INSERT INTO users VALUES (1, 'alice', TRUE);",
            "INSERT INTO users VALUES (2, 'bob', TRUE);",
            "INSERT INTO users VALUES (3, 'carol', FALSE);",
            "INSERT INTO users VALUES (4, 'dave', TRUE);",
            "INSERT INTO orders VALUES (101, 1, 30.5, FALSE);",
            "INSERT INTO orders VALUES (102, 1, 10, TRUE);",
            "INSERT INTO orders VALUES (103, 2, 50, FALSE);",
            "INSERT INTO orders VALUES (104, 3, 99, FALSE);",
            "INSERT INTO orders VALUES (105, 99, 5, FALSE);",
        ),
    )

    all_matches = runner.execute(
        "SELECT u.id, o.id FROM users AS u "
        "JOIN orders AS o ON u.id = o.user_id;"
    )
    assert all_matches.columns == ("u.id", "o.id")
    assert Counter(all_matches.rows) == Counter(
        ((1, 101), (1, 102), (2, 103), (3, 104))
    )

    filtered = runner.execute(
        "SELECT u.name, o.id, o.total FROM users u "
        "INNER JOIN orders o ON u.id = o.user_id "
        "WHERE u.active AND NOT o.cancelled;"
    )
    assert filtered.columns == ("u.name", "o.id", "o.total")
    assert Counter(filtered.rows) == Counter(
        (("alice", 101, 30.5), ("bob", 103, 50.0))
    )


def test_chained_join_executes_left_deep_plan_through_real_storage(tmp_path) -> None:
    """链式 JOIN 覆盖左深计划、累积 Schema 列位置和第二个 ON 的逻辑谓词。"""
    runner = _runner(tmp_path)
    _execute_all(
        runner,
        (
            "CREATE TABLE customers (id INT, name TEXT);",
            "CREATE TABLE orders (id INT, customer_id INT, paid BOOLEAN);",
            "CREATE TABLE items (id INT, order_id INT, sku TEXT, active BOOLEAN);",
            "INSERT INTO customers VALUES (1, 'alice');",
            "INSERT INTO customers VALUES (2, 'bob');",
            "INSERT INTO customers VALUES (3, 'carol');",
            "INSERT INTO orders VALUES (10, 1, TRUE);",
            "INSERT INTO orders VALUES (11, 1, FALSE);",
            "INSERT INTO orders VALUES (12, 2, TRUE);",
            "INSERT INTO orders VALUES (13, 99, TRUE);",
            "INSERT INTO items VALUES (100, 10, 'book', TRUE);",
            "INSERT INTO items VALUES (101, 10, 'pen', FALSE);",
            "INSERT INTO items VALUES (102, 11, 'lamp', TRUE);",
            "INSERT INTO items VALUES (103, 12, 'keyboard', TRUE);",
            "INSERT INTO items VALUES (104, 77, 'orphan', TRUE);",
        ),
    )

    result = runner.execute(
        "SELECT c.name, o.id, i.sku FROM customers c "
        "JOIN orders o ON c.id = o.customer_id "
        "JOIN items i ON o.id = i.order_id AND i.active "
        "WHERE o.paid;"
    )

    assert result.columns == ("c.name", "o.id", "i.sku")
    assert Counter(result.rows) == Counter(
        (("alice", 10, "book"), ("bob", 12, "keyboard"))
    )
