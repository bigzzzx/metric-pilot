"""Agent orchestration only: retrieve context, ask the model, invoke a tool."""
from langgraph.graph import END, START, StateGraph

from backend.model_client import complete_json
from backend.rag import prompt_context, retrieve
from backend.tools.sql_tool import run_readonly_sql


async def plan(state: dict) -> dict:
    context = prompt_context(state["message"])
    prompt = """You are a conversational data agent. Decide yourself whether to chat or query.
Use only the retrieved local catalog context below. Return JSON only:
{\"answer\":\"Chinese answer\",\"sql\":\"optional one SQLite SELECT/WITH statement\",\"should_analyze\":true/false}.
For chat, identity questions, or insufficient data, return an empty sql string.
RAG context:\n""" + context
    messages = [{"role":"system","content":prompt}] + state["history"][-8:] + [{"role":"user","content":state["message"]}]
    return {"plan": await complete_json(state["agent_id"], messages, state.get("model_id")), "rag": retrieve(state["message"])}


def execute_query(state: dict) -> dict:
    sql = str(state["plan"].get("sql") or "").strip()
    return {"sql": sql, "result": run_readonly_sql(sql)} if sql else {"sql":"", "result":[]}


def next_step(state: dict) -> str:
    return "query" if str(state["plan"].get("sql") or "").strip() else "finish"


builder = StateGraph(dict)
builder.add_node("plan", plan)
builder.add_node("query", execute_query)
builder.add_edge(START, "plan")
builder.add_conditional_edges("plan", next_step, {"query":"query", "finish":END})
builder.add_edge("query", END)
graph = builder.compile()


async def run_agent(message: str, agent_id: str, history: list[dict], model_id: str | None = None) -> dict:
    return await graph.ainvoke({"message":message, "agent_id":agent_id, "history":history, "model_id":model_id})
