"""HTTP routes only. Agent, RAG, model adapter, and tools live in separate modules."""
import json
import os
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.agent import run_agent
from backend.core import db, secrets


def init_storage():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS providers(id TEXT PRIMARY KEY,name TEXT,kind TEXT,base_url TEXT,api_key TEXT,enabled INTEGER);
    CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY,provider_id TEXT,model_id TEXT,display_name TEXT,enabled INTEGER);
    CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY,name TEXT,description TEXT,model_id TEXT,tools TEXT,system_prompt TEXT,enabled INTEGER);
    CREATE TABLE IF NOT EXISTS sessions(session_id TEXT,role TEXT,content TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    columns = {row["name"] for row in con.execute("PRAGMA table_info(agents)")}
    if "mcp_servers" not in columns: con.execute("ALTER TABLE agents ADD COLUMN mcp_servers TEXT DEFAULT '[]'")
    if "skills" not in columns: con.execute("ALTER TABLE agents ADD COLUMN skills TEXT DEFAULT '[]'")
    if not con.execute("SELECT 1 FROM providers").fetchone() and os.getenv("DEEPSEEK_API_KEY"):
        provider_id, model_id = str(uuid.uuid4()), str(uuid.uuid4())
        con.execute("INSERT INTO providers VALUES (?,?,?,?,?,1)", (provider_id,"DeepSeek","openai-compatible",os.getenv("DEEPSEEK_BASE_URL","https://api.deepseek.com"),secrets().encrypt(os.environ["DEEPSEEK_API_KEY"].encode()).decode()))
        con.execute("INSERT INTO models VALUES (?,?,?,?,1)", (model_id,provider_id,os.getenv("DEEPSEEK_MODEL","deepseek-v4-flash"),"DeepSeek V4 Flash"))
    default_model = con.execute("SELECT id FROM models WHERE enabled=1 LIMIT 1").fetchone()
    if default_model:
        configured_agents = [
            ("general-agent", "General Agent", "回答通用问题，不调用数据工具。", ["chat"], "你是一个友好、可靠的通用中文助手。回答日常问题时不要声称查询了业务数据。"),
            ("data-query-agent", "Data Query Agent", "检索数据资产知识并按需生成安全 SQL。", ["catalog_rag", "readonly_sql"], "你是严谨的数据查询智能体。只能依据 RAG 元数据和允许的工具处理数据问题。"),
            ("data-analysis-agent", "Data Analysis Agent", "基于可信查询结果生成业务分析。", ["catalog_rag", "query_result_analysis"], "你是业务数据分析师。只能依据可信查询结果和指标口径给出结论、限制与后续分析建议。"),
        ]
        for agent_id, name, description, tools, prompt in configured_agents:
            con.execute("INSERT OR IGNORE INTO agents(id,name,description,model_id,tools,system_prompt,enabled) VALUES (?,?,?,?,?,?,1)", (agent_id, name, description, default_model["id"], json.dumps(tools), prompt))
    # Local OpenAI-compatible runtimes are visible in provider management but
    # disabled until a user starts/configures one.
    for provider_id, name, kind, url in [
        ("local-ollama", "Ollama (Local)", "ollama", "http://localhost:11434/v1"),
        ("local-vllm", "vLLM (Local)", "vllm", "http://localhost:8000/v1"),
    ]:
        con.execute("INSERT OR IGNORE INTO providers VALUES (?,?,?,?,?,0)", (provider_id, name, kind, url, ""))
    # The pre-LangGraph single-agent configuration is obsolete. Runtime nodes
    # use the three role-specific configurations above.
    con.execute("DELETE FROM agents WHERE id='data-agent'")
    con.commit(); con.close()


class ProviderIn(BaseModel):
    name: str; base_url: str; api_key: str = ""; kind: str = "openai-compatible"; enabled: bool = True
class ModelIn(BaseModel):
    provider_id: str; model_id: str; display_name: str; enabled: bool = True
class ProviderUpdate(BaseModel):
    name: str; kind: str; base_url: str; api_key: str = ""; enabled: bool = True
class AgentUpdate(BaseModel):
    model_id: str; tools: list[str]; mcp_servers: list[str] = []; skills: list[str] = []; system_prompt: str; enabled: bool = True
class ChatIn(BaseModel):
    message: str = Field(min_length=1); session_id: str = "default"; model_id: str | None = None


app = FastAPI(title="Local Data Agent Platform")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["*"], allow_headers=["*"])
@app.on_event("startup")
def startup(): init_storage()
@app.get("/api/health")
def health(): return {"status":"ok","runtime":"fastapi-langgraph"}
@app.get("/api/providers")
def providers():
    con=db(); rows=[{"id":r["id"],"name":r["name"],"kind":r["kind"],"base_url":r["base_url"],"enabled":bool(r["enabled"]),"api_key_mask":"••••••••" if r["api_key"] else ""} for r in con.execute("SELECT * FROM providers")]; con.close(); return rows
@app.post("/api/providers")
def provider(x:ProviderIn):
    ident=str(uuid.uuid4()); con=db(); con.execute("INSERT INTO providers VALUES (?,?,?,?,?,?)",(ident,x.name,x.kind,x.base_url.rstrip("/"),secrets().encrypt(x.api_key.encode()).decode() if x.api_key else "",int(x.enabled))); con.commit(); con.close(); return {"id":ident,**x.model_dump(exclude={"api_key"})}
@app.put("/api/providers/{provider_id}")
def update_provider(provider_id: str, payload: ProviderUpdate):
    con=db(); old=con.execute("SELECT api_key FROM providers WHERE id=?", (provider_id,)).fetchone()
    if not old: con.close(); raise HTTPException(404, "Provider not found")
    encrypted=secrets().encrypt(payload.api_key.encode()).decode() if payload.api_key else old["api_key"]
    con.execute("UPDATE providers SET name=?,kind=?,base_url=?,api_key=?,enabled=? WHERE id=?", (payload.name,payload.kind,payload.base_url.rstrip("/"),encrypted,int(payload.enabled),provider_id)); con.commit(); con.close(); return {"id":provider_id, **payload.model_dump(exclude={"api_key"})}
@app.get("/api/models")
def models():
    con=db(); rows=[{**dict(r),"enabled":bool(r["enabled"])} for r in con.execute("SELECT * FROM models")]; con.close(); return rows
@app.post("/api/models")
def model(x:ModelIn):
    ident=str(uuid.uuid4()); con=db(); con.execute("INSERT INTO models VALUES (?,?,?,?,?)",(ident,x.provider_id,x.model_id,x.display_name,int(x.enabled))); con.commit(); con.close(); return {"id":ident,**x.model_dump()}
@app.put("/api/models/{model_id}")
def update_model(model_id: str, payload: ModelIn):
    con=db(); con.execute("UPDATE models SET provider_id=?,model_id=?,display_name=?,enabled=? WHERE id=?",(payload.provider_id,payload.model_id,payload.display_name,int(payload.enabled),model_id)); con.commit(); con.close(); return {"id":model_id,**payload.model_dump()}
@app.get("/api/agents")
def agents():
    con=db(); rows=[{**dict(r),"tools":json.loads(r["tools"]),"mcp_servers":json.loads(r["mcp_servers"] or "[]"),"skills":json.loads(r["skills"] or "[]"),"enabled":bool(r["enabled"])} for r in con.execute("SELECT * FROM agents")]; con.close(); return rows
@app.put("/api/agents/{agent_id}")
def update_agent(agent_id: str, payload: AgentUpdate):
    con=db()
    if not con.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
        con.close(); raise HTTPException(404, "Agent not found")
    if not con.execute("SELECT 1 FROM models WHERE id=?", (payload.model_id,)).fetchone():
        con.close(); raise HTTPException(404, "Model not found")
    con.execute("UPDATE agents SET model_id=?,tools=?,mcp_servers=?,skills=?,system_prompt=?,enabled=? WHERE id=?", (payload.model_id, json.dumps(payload.tools), json.dumps(payload.mcp_servers), json.dumps(payload.skills), payload.system_prompt, int(payload.enabled), agent_id))
    con.commit(); con.close(); return {"id":agent_id, **payload.model_dump()}
@app.post("/api/chat")
async def chat(x:ChatIn):
    con=db(); history=[dict(r) for r in con.execute("SELECT role,content FROM sessions WHERE session_id=? ORDER BY created_at",(x.session_id,))]; con.close()
    state=await run_agent(x.message,"data-query-agent",history,x.model_id); plan=state.get("plan",{}); final=state.get("final",plan); answer=str(final.get("answer") or "I have processed your request.")
    con=db(); con.execute("INSERT INTO sessions(session_id,role,content) VALUES (?,?,?)",(x.session_id,"user",x.message));
    if state.get("result") is not None: con.execute("INSERT INTO sessions(session_id,role,content) VALUES (?,?,?)",(x.session_id,"user","Tool result: "+json.dumps(state["result"],ensure_ascii=False)))
    con.execute("INSERT INTO sessions(session_id,role,content) VALUES (?,?,?)",(x.session_id,"assistant",answer)); con.commit(); con.close()
    sql,result=state.get("sql",""),state.get("result",[]); steps=[{"name":"RAG metadata retrieval","detail":f"Retrieved {len(state.get('rag',[]))} metadata / metric / SQL-example chunks"},{"name":"LLM planning","detail":"Model chose chat or tool usage"}]
    if sql: steps += [{"name":"SQL guardrail","detail":"Read-only and catalog allowlist passed"},{"name":"Local Athena simulation","detail":f"Returned {len(result)} rows"}]
    return {"answer":answer,"intent":"DATA_QUERY" if sql else "CHAT","sql":sql,"result":result,"analysis":answer if state.get("final") else "","catalog":[{"name":item["title"]} for item in state.get("rag",[])],"modelMode":"configured","steps":steps}
