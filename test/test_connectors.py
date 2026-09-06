from backend.connectors import ConnectorUnavailable, LocalSqliteConnector, connector_for


def test_local_sqlite_connector_discovers_demo_tables():
    result = LocalSqliteConnector().discover()

    assert result["status"] == "discovered"
    assert {table["name"] for table in result["tables"]} >= {"users", "orders", "products"}
    assert any(column["name"] == "order_id" for table in result["tables"] if table["name"] == "orders" for column in table["columns"])


def test_uninstalled_remote_connector_is_explicit():
    connector = connector_for("aws-glue")

    try:
        connector.test()
    except ConnectorUnavailable as exc:
        assert "not installed" in str(exc)
    else:
        raise AssertionError("An uninstalled remote connector must not claim it connected")
