"""LangGraph orchestration: generic chat or RAG-grounded data analysis."""
from langgraph.graph import END, START, StateGraph

from backend.model_client import complete_json
from backend.rag import prompt_context, retrieve
from backend.tools.sql_tool import run_readonly_sql


def conversation_context(state: dict, extra: str = "") -> list[dict]:
    """Every model call sees the user question, conversation and tool/RAG evidence."""
    messages = [{"role": "system", "content": extra}]
    messages.extend(state["history"][-10:])
    messages.append({"role": "user", "content": state["message"]})
    return messages


async def retrieve_rag(state: dict) -> dict:
    chunks = retrieve(state["message"], fallback=False)
    return {**state, "rag": chunks}


def route_domain(state: dict) -> str:
    return "data_plan" if state.get("rag") else "general"


async def general_agent(state: dict) -> dict:
    prompt = """You are the General Agent. Answer normal questions naturally in Chinese.
Do not claim to have queried business data and do not generate SQL. Return JSON only:
{"answer":"Chinese answer","sql":"","should_analyze":false}."""
    return {**state, "plan": await complete_json(state["agent_id"], conversation_context(state, prompt), state.get("model_id"))}


async def data_plan(state: dict) -> dict:
    prompt = """You are the Data Query Agent. The user question has relevant local data-lake evidence.
Use the RAG context below to decide whether a read-only query is needed. Return JSON only:
{"answer":"short Chinese acknowledgement or metadata answer","sql":"optional one SQLite SELECT/WITH","should_analyze":true/false}.
Never invent tables or fields. RAG context:\n""" + prompt_context(state["message"], state["rag"])
    return {**state, "plan": await complete_json(state["agent_id"], conversation_context(state, prompt), state.get("model_id"))}


def route_query(state: dict) -> str:
    return "query" if str(state["plan"].get("sql") or "").strip() else "finish"


def query_tool(state: dict) -> dict:
    sql = str(state["plan"].get("sql") or "").strip()
    return {**state, "sql": sql, "result": run_readonly_sql(sql)}


async def data_analyst(state: dict) -> dict:
    prompt = """You are the Data Analysis Agent. A safe SQL query has executed successfully.
Give a complete Chinese analysis based only on the supplied result and RAG evidence. Explain findings,
limits, and useful follow-up angles when appropriate; do not fabricate values. Return JSON only:
{"answer":"detailed Chinese analysis","sql":"","should_analyze":true}.
RAG context:\n""" + prompt_context(state["message"], state["rag"]) + "\n\nTool result:\n" + str(state["result"])
    analysis_messages = conversation_context(state, prompt)
    # Preserve tool output in the model-visible conversation history.
    analysis_messages.insert(-1, {"role": "user", "content": "Tool result (trusted local query): " + str(state["result"])})
    return {**state, "final": await complete_json(state["agent_id"], analysis_messages, state.get("model_id"))}


builder = StateGraph(dict)
builder.add_node("retrieve_rag", retrieve_rag)
builder.add_node("general", general_agent)
builder.add_node("data_plan", data_plan)
builder.add_node("query", query_tool)
builder.add_node("data_analyst", data_analyst)
builder.add_edge(START, "retrieve_rag")
builder.add_conditional_edges("retrieve_rag", route_domain, {"general":"general", "data_plan":"data_plan"})
builder.add_conditional_edges("data_plan", route_query, {"query":"query", "finish":END})
builder.add_edge("general", END)
builder.add_edge("query", "data_analyst")
builder.add_edge("data_analyst", END)
graph = builder.compile()


async def run_agent(message: str, agent_id: str, history: list[dict], model_id: str | None = None) -> dict:
    return await graph.ainvoke({"message": message, "agent_id": agent_id, "history": history, "model_id": model_id})
