from backend.catalog.metadata_semantics import column_semantics, table_semantics
from backend.catalog import publications, service
from backend.main import init_storage
from test.test_data_source_admin import factory_for, seed_users


def test_table_comment_maps_domain_layer_grain_and_sensitivity():
    result = table_semantics("业务域=供应链;分层=DWS;粒度=日期、商品;敏感字段=成本", "public")
    assert result == {
        "domain_id": "domain-supply-chain",
        "owner_group_id": "group-supply-chain-analysts",
        "warehouse_layer": "DWS",
        "data_grain": "日期、商品",
        "aliases": [],
        "sensitivity": "sensitive",
    }


def test_column_comment_maps_alias_role_and_masking():
    result = column_semantics("手机号;业务别名=联系电话;敏感级别=高;展示规则=仅保留前三后四", "phone", "text", False)
    assert result["display_name"] == "联系电话"
    assert result["aliases"] == ["联系电话"]
    assert result["sensitivity"] == "sensitive"
    assert result["classification"] == "phone_number"
    assert result["masking_strategy"] == "partial"


def test_primary_key_and_time_roles_are_derived_from_technical_metadata():
    assert column_semantics("订单主键", "order_id", "integer", True)["semantic_role"] == "identifier"
    assert column_semantics("下单时间", "order_time", "timestamp", False)["semantic_role"] == "time"


def test_discovery_persists_comment_semantics(tmp_path, monkeypatch):
    factory = factory_for(tmp_path / "metadata-semantics.db")
    init_storage(factory)
    seed_users(factory)
    connection = factory()
    connection.execute("""INSERT INTO data_sources(
        id,name,kind,endpoint,catalog_name,auth_type,credential,enabled,username
    ) VALUES ('supply','供应链源','postgresql','db:5432','warehouse','read_only_account','cipher',1,'reader')""")
    connection.commit(); connection.close()
    monkeypatch.setattr(publications, "db", factory)
    monkeypatch.setattr(service, "db", factory)

    publications.import_discovery_as_draft("supply", {
        "tables": [{
            "namespace": "dws", "name": "product_day",
            "description": "业务域=供应链;分层=DWS;粒度=日期、商品",
            "primary_key": ["day", "product_id"],
            "columns": [
                {"name": "product_id", "type": "integer", "is_primary_key": True,
                 "description": "商品主键;业务别名=商品ID"},
                {"name": "cost", "type": "numeric", "description": "成本金额"},
            ],
        }],
    }, "admin")

    connection = factory()
    asset = connection.execute("SELECT * FROM catalog_assets WHERE source_id='supply'").fetchone()
    columns = connection.execute("SELECT * FROM catalog_columns WHERE asset_id=? ORDER BY ordinal", (asset["id"],)).fetchall()
    connection.close()
    assert asset["domain_id"] == "domain-supply-chain"
    assert asset["warehouse_layer"] == "DWS"
    assert asset["data_grain"] == "日期、商品"
    assert columns[0]["display_name"] == "商品ID"
    assert columns[0]["semantic_role"] == "identifier"
    assert columns[1]["semantic_role"] == "measure"
    assert not service.can_access("analyst", service.DISCOVER, asset_id=asset["id"])
    connection = factory()
    connection.execute("INSERT INTO catalog_group_memberships(group_id,user_id) VALUES ('group-supply-chain-analysts','analyst')")
    connection.commit(); connection.close()
    assert service.can_access("analyst", service.DISCOVER, asset_id=asset["id"])
