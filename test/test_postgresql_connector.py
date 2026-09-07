from backend.data_sources.base import ConnectorConfig, ConnectorUnavailable
from backend.data_sources.postgresql import PostgresConnector, parse_endpoint


class FakeCursor:
    def __init__(self):
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql):
        if "current_database()" in sql:
            self.rows = [{"database_name": "warehouse", "username": "reader", "read_only": "on", "server_version": "16.14"}]
        elif "c.relname AS name" in sql:
            self.rows = [
                {"namespace": "ods", "name": "orders", "description": "订单原始表"},
                {"namespace": "ods", "name": "users", "description": "用户原始表"},
            ]
        elif "FROM pg_attribute a" in sql:
            self.rows = [
                {"namespace": "ods", "table_name": "orders", "name": "order_id", "data_type": "bigint", "ordinal": 1, "nullable": False, "description": "订单编号"},
                {"namespace": "ods", "table_name": "orders", "name": "user_id", "data_type": "bigint", "ordinal": 2, "nullable": False, "description": "用户编号"},
                {"namespace": "ods", "table_name": "users", "name": "user_id", "data_type": "bigint", "ordinal": 1, "nullable": False, "description": "用户编号"},
            ]
        elif "con.contype = 'p'" in sql:
            self.rows = [
                {"namespace": "ods", "table_name": "orders", "column_name": "order_id", "position": 1},
                {"namespace": "ods", "table_name": "users", "column_name": "user_id", "position": 1},
            ]
        elif "con.contype = 'f'" in sql:
            self.rows = [{
                "from_namespace": "ods", "from_table": "orders", "from_column": "user_id",
                "to_namespace": "ods", "to_table": "users", "to_column": "user_id", "position": 1,
            }]
        else:
            raise AssertionError(f"Unexpected SQL: {sql}")

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return FakeCursor()


def config():
    return ConnectorConfig(
        kind="postgresql", endpoint="db.internal:5433", catalog_name="warehouse",
        username="reader", password="secret",
    )


def test_parse_endpoint_accepts_host_port_and_rejects_embedded_credentials():
    assert parse_endpoint("db.internal:5433") == ("db.internal", 5433)
    assert parse_endpoint("postgresql://db.internal") == ("db.internal", 5432)
    try:
        parse_endpoint("postgresql://user:secret@db.internal/warehouse")
    except ConnectorUnavailable as exc:
        assert "dedicated fields" in str(exc)
    else:
        raise AssertionError("Credentials embedded in an endpoint must be rejected")


def test_connection_is_forced_read_only():
    captured = {}

    def connect(**kwargs):
        captured.update(kwargs)
        return FakeConnection()

    result = PostgresConnector(config(), connect_factory=connect).test()

    assert result["status"] == "connected"
    assert result["database"] == "warehouse"
    assert "default_transaction_read_only=on" in captured["options"]
    assert captured["connect_timeout"] == 5


def test_discovery_returns_namespaces_comments_keys_and_relationships():
    result = PostgresConnector(config(), connect_factory=lambda **_kwargs: FakeConnection()).discover()

    assert result["table_count"] == 2
    assert result["relationship_count"] == 1
    orders = next(table for table in result["tables"] if table["name"] == "orders")
    assert orders["namespace"] == "ods"
    assert orders["description"] == "订单原始表"
    assert orders["primary_key"] == ["order_id"]
    assert next(column for column in orders["columns"] if column["name"] == "order_id")["is_primary_key"] is True
    assert result["relationships"][0]["from_column"] == "user_id"
