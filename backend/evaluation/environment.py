"""Isolated catalog, ACL and SQL tools; live mode only shares model configuration."""
import sqlite3
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from fastapi import HTTPException

from backend.evaluation.fixtures import create_warehouse

SQL_ATTEMPTS = []


@contextmanager
def evaluation_environment(directory):
    from backend import agent
    from backend.main import init_storage
    from backend.catalog import rag_documents, retrieval, service
    from backend.hybrid_retrieval import bm25_rank
    from backend.tools import sql_tool
    from backend.tools.models import ToolResult, ToolStatus

    platform, warehouse = directory / "platform.db", directory / "warehouse.db"
    create_warehouse(warehouse)

    def factory(path=None):
        connection = sqlite3.connect(warehouse if path is not None else platform)
        connection.row_factory = sqlite3.Row
        return connection

    init_storage(factory)
    con = factory()
    # The fixture catalog must describe only tables actually created above.
    con.execute("""UPDATE catalog_assets SET publish_status='draft'
                   WHERE physical_name NOT IN ('users','products','orders','dwd_payment_detail','ads_campaign_roi')""")
    con.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES ('eval-analyst','eval-analyst','not-a-login',1)")
    con.execute("INSERT INTO catalog_group_memberships(group_id,user_id) VALUES ('group-ecommerce-analysts','eval-analyst')")
    con.execute("""INSERT INTO catalog_metrics(id,name,display_name,domain_id,description,definition_sql,formula,grain,publish_status,certification_status)
                 VALUES ('metric-gmv','gmv','GMV','domain-ecommerce','已支付订单金额之和，退款与取消状态排除。',
                         'SUM(orders.amount) WHERE orders.status=''PAID''','已支付订单金额之和','订单','published','certified')""")
    con.commit()
    con.close()

    class FixtureTools:
        def invoke(self, tool_id, payload, context):
            try:
                if tool_id == "catalog_rag":
                    docs = rag_documents.authorized_catalog_documents(context.user_id)
                    rows = bm25_rank(payload["query"], docs, payload.get("limit", 8))
                    data = {"chunks": rows}
                elif tool_id == "readonly_sql":
                    SQL_ATTEMPTS.append({"sql": payload["sql"]})
                    data = {"rows": sql_tool.run_readonly_sql(payload["sql"], context.user_id)}
                else:
                    raise AssertionError("Evaluation cannot execute mutations or external tools")
                return ToolResult(status=ToolStatus.SUCCESS, tool_id=tool_id, call_id=context.call_id, data=data)
            except HTTPException as exc:
                if tool_id == "readonly_sql":
                    SQL_ATTEMPTS[-1]["error"] = str(exc.detail)
                return ToolResult(status=ToolStatus.DENIED if exc.status_code == 403 else ToolStatus.FAILED,
                                  tool_id=tool_id, call_id=context.call_id,
                                  error={"code": "permission_denied" if exc.status_code == 403 else "sql_error", "message": str(exc.detail)})

    with ExitStack() as stack:
        for module in (rag_documents, retrieval, service, sql_tool):
            stack.enter_context(patch.object(module, "db", factory))
        stack.enter_context(patch.object(agent, "tool_executor", FixtureTools()))
        stack.enter_context(patch.object(agent, "has_tool", lambda _agent, tool: tool in {"chat", "catalog_rag", "readonly_sql", "query_result_analysis"}))
        stack.enter_context(patch.object(agent, "tool_catalog_for_router", lambda: []))
        stack.enter_context(patch.object(agent, "bound_skill_summaries", lambda: []))
        stack.enter_context(patch.object(agent, "selected_skill_context", lambda *_args: ""))
        yield factory
