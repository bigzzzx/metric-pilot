"""Stateful LangGraph orchestration for general chat and data analysis."""
import json
from typing import Literal, TypedDict

from fastapi import HTTPException
from langgraph.graph import END, START, StateGraph

from backend.model_client import complete_json
from backend.rag import prompt_context, retrieve
from backend.tools.sql_tool import run_readonly_sql


class AgentState(TypedDict, total=False):
    message: str
    agent_id: str
    model_id: str | None
    history: list[dict]
    last_query_result: list[dict] | None
    route: Literal["general", "data_query", "data_analysis", "etl"]
    rag: list[dict]
    plan: dict
    sql: str
    result: list[dict]
    query_error: str
    repair_attempts: int
    final: dict


def messages(state: AgentState, system: str, include_result: bool = False) -> list[dict]:
    context = [{"role": "system", "content": system}]
    context.extend(state["history"][-10:])
    if include_result and state.get("last_query_result") is not None:
        context.append({"role": "user", "content": "Most recent trusted query result: " + json.dumps(state["last_query_result"], ensure_ascii=False)})
    context.append({"role": "user", "content": state["message"]})
    return context


async def router(state: AgentState) -> AgentState:
    has_result = state.get("last_query_result") is not None
    prompt = f'''You are a strict request router. Recent successful query result exists: {has_result}.
Return JSON only: {{"route":"general|data_query|data_analysis|etl"}}.
Use data_analysis ONLY when a recent query result exists AND the user is asking to interpret, compare,
explain, or extend that result. Use data_query for metadata, metrics, business data, tables, fields or SQL.
Use general for unrelated general questions.''' 
    decision = await complete_json(state["agent_id"], messages(state, prompt), state.get("model_id"))
    route = decision.get("route", "general")
    if route == "data_analysis" and not has_result: route = "data_query"
    if route not in {"general", "data_query", "data_analysis", "etl"}: route = "general"
    return {**state, "route": route}


def route_after_router(state: AgentState) -> str:
    return state["route"]


async def general_agent(state: AgentState) -> AgentState:
    prompt = 'You are the General Agent. Answer naturally in Chinese. Do not claim to query platform data. Return JSON only: {"answer":"Chinese answer"}.'
    return {**state, "final": await complete_json(state["agent_id"], messages(state, prompt), state.get("model_id"))}


async def rag_retrieve(state: AgentState) -> AgentState:
    return {**state, "rag": retrieve(state["message"], fallback=False)}


async def data_query_agent(state: AgentState) -> AgentState:
    evidence = prompt_context(state["message"], state.get("rag", []))
    prompt = '''You are the Data Query Agent. Use only the supplied RAG evidence. Answer metadata questions directly;
generate SQL only if data must be queried. SQL must be one SQLite SELECT/WITH statement. Return JSON only:
{"answer":"Chinese answer","sql":"optional SQL"}. RAG evidence:\n''' + evidence
    return {**state, "plan": await complete_json(state["agent_id"], messages(state, prompt), state.get("model_id"))}


def route_after_plan(state: AgentState) -> str:
    return "query" if str(state["plan"].get("sql") or "").strip() else "finish"


def query_tool(state: AgentState) -> AgentState:
    sql = str(state["plan"].get("sql") or "").strip()
    try:
        return {**state, "sql": sql, "result": run_readonly_sql(sql)}
    except HTTPException as exc:
        return {**state, "sql": sql, "query_error": str(exc.detail), "repair_attempts": state.get("repair_attempts", 0)}


def route_after_query(state: AgentState) -> str:
    if state.get("query_error") and state.get("repair_attempts", 0) < 1: return "repair"
    return "analysis" if not state.get("query_error") else "query_failed"


async def repair_sql(state: AgentState) -> AgentState:
    prompt = f'''You are SQL Repair Agent. Fix the failed SQLite SQL using RAG context only. Error: {state["query_error"]}.
Original SQL: {state["sql"]}. Return JSON only: {{"sql":"one corrected SELECT/WITH"}}.\nRAG:\n''' + prompt_context(state["message"], state.get("rag", []))
    repaired = await complete_json(state["agent_id"], messages(state, prompt), state.get("model_id"))
    return {**state, "plan": {"sql": repaired.get("sql", "")}, "repair_attempts": state.get("repair_attempts", 0) + 1, "query_error": ""}


async def data_analysis_agent(state: AgentState) -> AgentState:
    result = state.get("result", state.get("last_query_result", []))
    prompt = '''You are the Data Analysis Agent. Give a rigorous Chinese analysis using only RAG evidence and trusted SQL result.
You may explain implications and suggest next analyses, but never invent data. Return JSON only:
{"answer":"full Chinese analysis","summary":"core conclusion","insights":["..."],"limitations":["..."],"follow_up_questions":["..."]}.
RAG evidence:\n''' + prompt_context(state["message"], state.get("rag", [])) + "\nTrusted result:\n" + json.dumps(result, ensure_ascii=False)
    return {**state, "result": result, "final": await complete_json(state["agent_id"], messages(state, prompt, include_result=True), state.get("model_id"))}


async def etl_agent(state: AgentState) -> AgentState:
    prompt = 'You are the ETL Agent. Explain that ETL task execution is a controlled extension point in this demo. Return JSON only: {"answer":"Chinese answer"}.'
    return {**state, "final": await complete_json(state["agent_id"], messages(state, prompt), state.get("model_id"))}


def failed_query(state: AgentState) -> AgentState:
    return {**state, "final": {"answer": "SQL 在一次自动修复后仍无法安全执行：" + state.get("query_error", "unknown error")}}


builder = StateGraph(AgentState)
builder.add_node("router", router); builder.add_node("general", general_agent); builder.add_node("rag", rag_retrieve)
builder.add_node("data_query", data_query_agent); builder.add_node("query", query_tool); builder.add_node("repair", repair_sql)
builder.add_node("data_analysis", data_analysis_agent); builder.add_node("etl", etl_agent); builder.add_node("query_failed", failed_query)
builder.add_edge(START, "router")
builder.add_conditional_edges("router", route_after_router, {"general":"general", "data_query":"rag", "data_analysis":"rag", "etl":"etl"})
builder.add_conditional_edges("rag", lambda state: "analysis" if state["route"] == "data_analysis" else "query", {"analysis":"data_analysis", "query":"data_query"})
builder.add_conditional_edges("data_query", route_after_plan, {"query":"query", "finish":END})
builder.add_conditional_edges("query", route_after_query, {"repair":"repair", "analysis":"data_analysis", "query_failed":"query_failed"})
builder.add_edge("repair", "query"); builder.add_edge("general", END); builder.add_edge("etl", END); builder.add_edge("data_analysis", END); builder.add_edge("query_failed", END)
graph = builder.compile()


def last_result(history: list[dict]) -> list[dict] | None:
    for item in reversed(history):
        text = item.get("content", "")
        if text.startswith("Tool result: "):
            try: return json.loads(text.removeprefix("Tool result: "))
            except json.JSONDecodeError: return None
    return None


async def run_agent(message: str, agent_id: str, history: list[dict], model_id: str | None = None) -> AgentState:
    return await graph.ainvoke({"message":message, "agent_id":agent_id, "history":history, "model_id":model_id, "last_query_result":last_result(history), "repair_attempts":0})
