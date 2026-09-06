import asyncio
import json

from backend.evaluation.environment import evaluation_environment
from backend.query_quality import schema_lookup
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
