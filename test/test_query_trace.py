from backend.query_trace import build_query_trace


def test_query_trace_is_bounded_and_omits_prompts_credentials_and_rows():
    state = {
        "route": "data_query", "route_source": "model", "contextual_follow_up": True,
        "rag": [{
            "id": f"source-{index}", "kind": "table", "title": f"表 {index}",
            "asset_ids": [f"asset-{index}"], "domain_id": "domain-ecommerce",
            "content": "secret prompt content",
        } for index in range(20)],
        "verified_schemas": {"tables": [{
            "asset_id": "asset-orders", "namespace": "ods", "name": "orders",
            "source_id": "source-pg", "source_name": "测试数仓", "catalog_version": 3,
            "columns": [{"name": "sensitive_value"}],
        }]},
        "plan": {"metric_refs": [{"id": "metric-gmv", "version": "a" * 64}],
                 "filters": ["status = 'PAID'"], "join_relationships": []},
        "result": [{"secret": "must not enter trace"}],
        "tool_result": {"status": "success", "data": {"rows": [{"secret": "hidden"}]}},
        "node_trace": [{"node": f"n{index}", "label": "step", "duration_ms": index, "status": "success"}
                       for index in range(30)],
    }
    trace = build_query_trace(state)
    assert len(trace["sources"]) == 12
    assert len(trace["nodes"]) == 16
    assert trace["verified_tables"][0] == {
        "asset_id": "asset-orders", "name": "ods.orders", "source_id": "source-pg",
        "source_name": "测试数仓", "catalog_version": 3,
    }
    assert trace["execution"]["row_count"] == 1
    rendered = str(trace)
    assert "secret prompt content" not in rendered
    assert "must not enter trace" not in rendered
    assert "sensitive_value" not in rendered


def test_general_chat_without_data_evidence_has_no_query_trace():
    assert build_query_trace({"route": "general", "final": {"answer": "你好"}}) is None
