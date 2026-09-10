import asyncio
import json

from backend.evaluation.environment import evaluation_environment
from backend.query_quality import schema_lookup
from backend.query_quality.planner_context import candidate_tables
from backend.tools.models import ToolExecutionContext


def test_schema_is_authorized_complete_and_row_free(tmp_path):
    with evaluation_environment(tmp_path):
        context = ToolExecutionContext(user_id="eval-analyst", agent_id="data-query-agent")
        result = schema_lookup.lookup_schema(schema_lookup.SchemaLookupInput(tables=["orders", "users", "ads_campaign_roi"]), context)
        assert {t["name"] for t in result["tables"]} == {"orders", "users"}
        users = next(t for t in result["tables"] if t["name"] == "users")
        assert "name" not in {c["name"] for c in users["columns"]}
        assert "rows" not in str(result) and result["unavailable"]
        assert "ads_campaign_roi" not in str(result)


def test_schema_does_not_leak_to_other_user(tmp_path):
    with evaluation_environment(tmp_path):
        result = schema_lookup.lookup_schema(schema_lookup.SchemaLookupInput(tables=["orders"]),
            ToolExecutionContext(user_id="no-access", agent_id="data-query-agent"))
        assert result["tables"] == []
        assert "order_date" not in str(result)


def test_fixture_metadata_does_not_claim_a_nonexistent_payment_date(tmp_path):
    with evaluation_environment(tmp_path):
        result = schema_lookup.lookup_schema(schema_lookup.SchemaLookupInput(tables=["dwd_payment_detail"]),
            ToolExecutionContext(user_id="eval-analyst", agent_id="data-query-agent"))
        assert result["tables"]
        assert "pay_date" not in str(result)


def test_binding_migration_does_not_reenable_disabled_tool(tmp_path):
    with evaluation_environment(tmp_path) as factory:
        con = factory()
        con.execute("INSERT INTO agents(id,tools) VALUES ('data-query-agent',?)", (json.dumps(["catalog_rag", "readonly_sql"]),))
        schema_lookup.migrate_schema_binding(con)
        assert "catalog_schema" in con.execute("SELECT tools FROM agents WHERE id='data-query-agent'").fetchone()[0]
        con.execute("UPDATE agents SET tools='[]' WHERE id='data-query-agent'")
        schema_lookup.migrate_schema_binding(con)
        assert con.execute("SELECT tools FROM agents WHERE id='data-query-agent'").fetchone()[0] == "[]"
        con.close()


def test_model_can_request_schema_once_before_sql(tmp_path, monkeypatch):
    from backend import agent
    prompts = []
    async def complete(_id, messages, schema, _model):
        prompts.append(messages[0]["content"])
        if len(prompts) == 1:
            return schema(answer="核实订单结构", schema_request=["orders"])
        assert "order_date" in prompts[-1]
        return schema(answer="查询", sql="SELECT COUNT(*) FROM orders")
    with evaluation_environment(tmp_path):
        monkeypatch.setattr(agent, "complete_structured", complete)
        result = asyncio.run(agent.data_query_agent({"message": "订单数", "history": [], "user_id": "eval-analyst", "rag": []}))
        assert result["plan"]["sql"] and len(prompts) == 2


def test_schema_request_merges_with_initially_verified_tables(tmp_path, monkeypatch):
    from backend import agent
    calls = []
    async def complete(_id, messages, schema, _model):
        calls.append(messages)
        if len(calls) == 1:
            return schema(answer="补查用户", schema_request=["users"])
        return schema(answer="查询", sql="SELECT COUNT(*) FROM orders JOIN users ON orders.user_id=users.user_id")
    rag = [{"kind": "table", "title": "订单", "content": "orders", "asset_ids": ["orders"]}]
    with evaluation_environment(tmp_path):
        monkeypatch.setattr(agent, "complete_structured", complete)
        result = asyncio.run(agent.data_query_agent({
            "message": "订单关联用户", "history": [], "user_id": "eval-analyst", "rag": rag,
        }))
        assert {table["name"] for table in result["verified_schemas"]["tables"]} == {"orders", "users"}


def test_repeated_schema_requests_stop_without_loop(tmp_path, monkeypatch):
    from backend import agent
    calls = []
    async def complete(_id, messages, schema, _model):
        calls.append(1)
        return schema(answer="继续查", schema_request=["orders"])
    with evaluation_environment(tmp_path):
        monkeypatch.setattr(agent, "complete_structured", complete)
        result = asyncio.run(agent.data_query_agent({"message": "订单数", "history": [], "user_id": "eval-analyst", "rag": []}))
        assert result["plan"]["sql"] is None and len(calls) == 2


def test_candidate_tables_rank_question_terms_over_repeated_generic_hits():
    rag = [
        {"kind": "table", "title": "交易日报", "content": "订单 GMV 汇总", "asset_ids": ["daily"]},
        {"kind": "column", "title": "订单金额", "content": "订单金额", "asset_ids": ["orders"]},
        {"kind": "table", "title": "订单", "content": "订单状态 order_id", "asset_ids": ["orders"]},
        {"kind": "table", "title": "订单明细", "content": "订单明细 商品 product_id 件数", "asset_ids": ["items"]},
        {"kind": "relation", "title": "明细关联商品", "content": "订单明细关联商品，用商品品类分析", "asset_ids": ["items", "products"]},
        {"kind": "relation", "title": "明细关联订单", "content": "订单明细关联订单，用订单状态分析", "asset_ids": ["items", "orders"]},
    ]
    selected = candidate_tables(rag, "关联订单明细和商品，按品类统计件数")
    assert set(selected) == {"orders", "items", "products"}
