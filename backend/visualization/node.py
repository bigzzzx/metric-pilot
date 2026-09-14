from backend.tools.executor import executor
from backend.tools.models import ToolExecutionContext, ToolStatus
from backend.visualization.models import VisualizationRequest
from backend.visualization.tool import bind_result


def visualization_node(state):
    rows = state.get("result") or []
    if state.get("query_error") or state.get("permission_error") or not rows:
        return {**state, "visualizations": []}
    request = VisualizationRequest.model_validate(state.get("visualization_request") or {})
    context = ToolExecutionContext(user_id=state.get("user_id") or "local-user",
                                   conversation_id=state.get("conversation_id") or "",
                                   agent_id="data-analysis-agent")
    with bind_result(rows, context) as ident:
        outcome = executor.invoke("create_visualization", {**request.model_dump(), "result_id": ident}, context)
    if outcome.status != ToolStatus.SUCCESS:
        return {**state, "visualizations": [], "visualization_notice": outcome.error.message if outcome.error else "图表暂不可用，已保留查询结果。"}
    return {**state, "visualizations": [outcome.data], "visualization_notice": "",
            "visualization_reused": bool(state.get("visualization_only"))}


def init_bindings(connection_factory):
    import json
    from contextlib import closing
    with closing(connection_factory()) as con:
        version = "query_visualization_v1"
        if con.execute("SELECT 1 FROM app_migrations WHERE version=?", (version,)).fetchone():
            return
        row = con.execute("SELECT tools FROM agents WHERE id='data-analysis-agent'").fetchone()
        if not row:
            return
        if row:
            tools = json.loads(row["tools"] or "[]")
            if "create_visualization" not in tools:
                tools.append("create_visualization")
                con.execute("UPDATE agents SET tools=? WHERE id='data-analysis-agent'", (json.dumps(tools),))
        con.execute("INSERT INTO app_migrations(version,applied_at) VALUES (?,CURRENT_TIMESTAMP)", (version,))
        con.commit()
