"""The single authoritative topology for both chat API entry points."""
import asyncio
import inspect
import time

from langgraph.graph import END, START, StateGraph

from backend.graph_runtime.events import emit
from backend.observability import log_event
from backend.model_reliability import model_node

NODES = {
    "router": ("router", "正在理解问题"),
    "memory_search": ("retrieve_long_term_memory", "正在检索相关长期记忆"),
    "general": ("general_agent", "正在生成回答"),
    "rag": ("rag_retrieve", "正在检索数据目录"),
    "data_query": ("data_query_agent", "正在规划查询"),
    "query": ("query_tool", "正在执行只读查询"),
    "repair": ("repair_sql", "正在修复 SQL"),
    "data_analysis": ("data_analysis_agent", "正在分析可信查询结果"),
    "data_write": ("data_write_agent", "正在处理写入任务"),
    "etl": ("etl_agent", "正在处理 ETL 任务"),
    "query_failed": ("failed_query", "查询未完成"),
    "mcp_tool": ("execute_mcp_tool", "正在调用 MCP 工具"),
}


def build_graph(namespace: dict):
    from backend.attachments.agent import attachment_agent
    builder = StateGraph(namespace["AgentOverallState"],
                         input_schema=namespace["AgentInputState"],
                         output_schema=namespace["AgentOutputState"])

    def wrap(node, function_name, label):
        async def execute(state):
            emit({"event": "status", "label": label, "node": node})
            started = time.perf_counter()
            function = namespace[function_name]
            token = model_node.set(node)
            status = "success"
            try:
                if inspect.iscoroutinefunction(function):
                    result = await function(state)
                else:
                    result = await asyncio.to_thread(function, state)
            except BaseException as exc:
                status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                log_event("graph.node_failed", level=30, node=node, error_type=type(exc).__name__,
                          error_code=getattr(exc, "code", None))
                raise
            finally:
                duration = round((time.perf_counter() - started) * 1000, 2)
                emit({"event": "node_completed", "node": node, "duration_ms": duration, "status": status})
                log_event("graph.node_completed", node=node, duration_ms=duration, status=status)
                model_node.reset(token)
            trace = [*state.get("node_trace", []), {
                "node": node, "label": label, "duration_ms": duration, "status": status,
            }]
            return {**result, "node_trace": trace}
        return execute

    for node, (function, label) in NODES.items():
        builder.add_node(node, wrap(node, function, label))
    namespace['attachment_agent'] = attachment_agent
    builder.add_node('attachment', wrap('attachment', 'attachment_agent', '正在读取会话附件'))
    builder.add_edge('attachment', END)
    builder.add_edge(START, "router")
    builder.add_conditional_edges("router", namespace["route_after_router"], namespace["ROUTER_DESTINATIONS"])
    builder.add_conditional_edges("memory_search", namespace["route_after_memory"], namespace["ROUTER_DESTINATIONS"])
    builder.add_conditional_edges("rag", lambda s: "analysis" if s["route"] == "data_analysis" else "query",
                                  {"analysis": "data_analysis", "query": "data_query"})
    builder.add_conditional_edges("data_query", namespace["route_after_plan"], {"query": "query", "finish": END})
    builder.add_conditional_edges("query", namespace["route_after_query"],
                                  {"repair": "repair", "analysis": "data_analysis", "query_failed": "query_failed"})
    builder.add_edge("repair", "query")
    for node in ("general", "data_write", "etl", "data_analysis", "query_failed", "mcp_tool"):
        builder.add_edge(node, END)
    return builder.compile()
