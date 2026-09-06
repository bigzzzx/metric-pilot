import sqlite3

import pytest

from backend.tools.errors import ToolRuntimeError
from backend.tools.models import SqlWriteInput, ToolExecutionContext
from backend.tools.sql_write_tool import execute_sql_write, prepare_sql_write


def _database(tmp_path):
    path = tmp_path / "write-demo.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE orders(
          order_id INTEGER PRIMARY KEY,
          status TEXT NOT NULL,
          amount REAL NOT NULL
        );
        INSERT INTO orders VALUES (1001, 'paid', 88.5);
        INSERT INTO orders VALUES (1002, 'paid', 120.0);
        CREATE TABLE users(user_id INTEGER PRIMARY KEY, name TEXT, city TEXT);
        INSERT INTO users VALUES (1, '王芳', '上海');
        """
    )
    connection.commit()
    connection.close()
    return path


def _context():
    return ToolExecutionContext(
        user_id="admin-1",
        agent_id="data-query-agent",
        conversation_id="conversation-1",
        call_id="write-call-1",
        resource_type="table",
        resource_id="orders",
    )


def test_update_is_prepared_and_executed_with_bound_parameters(tmp_path):
    path = _database(tmp_path)
    prepared = prepare_sql_write(
        {
            "operation": "update",
            "table": "orders",
            "values": {"status": "cancelled"},
            "where": [{"column": "order_id", "value": 1001}],
            "summary": "取消订单 1001",
        },
        path,
    )

    assert prepared["estimated_rows"] == 1
    output = execute_sql_write(SqlWriteInput.model_validate(prepared), _context(), path)

    assert output["affected_rows"] == 1
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT status FROM orders WHERE order_id=1001"
    ).fetchone()[0] == "cancelled"
    connection.close()


def test_update_and_delete_require_a_where_clause(tmp_path):
    path = _database(tmp_path)

    with pytest.raises(ToolRuntimeError) as exc:
        prepare_sql_write(
            {
                "operation": "update",
                "table": "orders",
                "values": {"status": "cancelled"},
                "where": [],
                "summary": "错误的全表更新",
            },
            path,
        )

    assert exc.value.code == "unsafe_write"


def test_unknown_tables_and_columns_are_rejected(tmp_path):
    path = _database(tmp_path)

    with pytest.raises(ToolRuntimeError) as exc:
        prepare_sql_write(
            {
                "operation": "update",
                "table": "orders",
                "values": {"secret_column": "x"},
                "where": [{"column": "order_id", "value": 1001}],
                "summary": "修改不存在字段",
            },
            path,
        )

    assert exc.value.code == "invalid_write_target"


def test_approval_becomes_stale_when_matching_rows_change(tmp_path):
    path = _database(tmp_path)
    prepared = prepare_sql_write(
        {
            "operation": "update",
            "table": "orders",
            "values": {"status": "cancelled"},
            "where": [{"column": "status", "value": "paid"}],
            "summary": "取消所有已支付订单",
        },
        path,
    )
    assert prepared["estimated_rows"] == 2
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO orders VALUES (1003, 'paid', 30.0)")
    connection.commit()
    connection.close()

    with pytest.raises(ToolRuntimeError) as exc:
        execute_sql_write(SqlWriteInput.model_validate(prepared), _context(), path)

    assert exc.value.code == "approval_stale"
    connection = sqlite3.connect(path)
    assert connection.execute(
        "SELECT count(*) FROM orders WHERE status='cancelled'"
    ).fetchone()[0] == 0
    connection.close()


def test_insert_treats_user_values_as_data_not_sql(tmp_path):
    path = _database(tmp_path)
    dangerous_name = "Robert'); DROP TABLE orders;--"
    prepared = prepare_sql_write(
        {
            "operation": "insert",
            "table": "users",
            "values": {"user_id": 2, "name": dangerous_name, "city": "杭州"},
            "where": [],
            "summary": "添加用户",
        },
        path,
    )
    output = execute_sql_write(
        SqlWriteInput.model_validate(prepared),
        _context().model_copy(update={"resource_id": "users"}),
        path,
    )

    assert output["affected_rows"] == 1
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT name FROM users WHERE user_id=2").fetchone()[0] == dangerous_name
    assert connection.execute("SELECT count(*) FROM orders").fetchone()[0] == 2
    connection.close()
