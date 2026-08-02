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
    if not con.execute("SELECT 1 FROM providers").fetchone() and os.getenv("DEEPSEEK_API_KEY"):
        provider_id, model_id = str(uuid.uuid4()), str(uuid.uuid4())
        con.execute("INSERT INTO providers VALUES (?,?,?,?,?,1)", (provider_id,"DeepSeek","openai-compatible",os.getenv("DEEPSEEK_BASE_URL","https://api.deepseek.com"),secrets().encrypt(os.environ["DEEPSEEK_API_KEY"].encode()).decode()))
        con.execute("INSERT INTO models VALUES (?,?,?,?,1)", (model_id,provider_id,os.getenv("DEEPSEEK_MODEL","deepseek-v4-flash"),"DeepSeek V4 Flash"))
        con.execute("INSERT INTO agents VALUES (?,?,?,?,?,?,1)", ("data-agent","Data Agent","Local data analysis agent",model_id,json.dumps(["catalog_rag","readonly_sql","etl"]),"You are a careful Chinese data agent."))
    con.commit(); con.close()


class ProviderIn(BaseModel):
    name: str; base_url: str; api_key: str = ""; kind: str = "openai-compatible"; enabled: bool = True
class ModelIn(BaseModel):
    provider_id: str; model_id: str; display_name: str; enabled: bool = True
class ChatIn(BaseModel):
    message: str = Field(min_length=1); session_id: str = "default"; agent_id: str = "data-agent"; model_id: str | None = None


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
@app.get("/api/models")
def models():
    con=db(); rows=[{**dict(r),"enabled":bool(r["enabled"])} for r in con.execute("SELECT * FROM models")]; con.close(); return rows
@app.post("/api/models")
def model(x:ModelIn):
    ident=str(uuid.uuid4()); con=db(); con.execute("INSERT INTO models VALUES (?,?,?,?,?)",(ident,x.provider_id,x.model_id,x.display_name,int(x.enabled))); con.commit(); con.close(); return {"id":ident,**x.model_dump()}
@app.get("/api/agents")
def agents():
    con=db(); rows=[{**dict(r),"tools":json.loads(r["tools"]),"enabled":bool(r["enabled"])} for r in con.execute("SELECT * FROM agents")]; con.close(); return rows
@app.post("/api/chat")
async def chat(x:ChatIn):
    con=db(); history=[dict(r) for r in con.execute("SELECT role,content FROM sessions WHERE session_id=? ORDER BY created_at",(x.session_id,))]; con.close()
    state=await run_agent(x.message,x.agent_id,history,x.model_id); plan=state.get("plan",{}); final=state.get("final",plan); answer=str(final.get("answer") or "I have processed your request.")
    con=db(); con.execute("INSERT INTO sessions(session_id,role,content) VALUES (?,?,?)",(x.session_id,"user",x.message));
    if state.get("result") is not None: con.execute("INSERT INTO sessions(session_id,role,content) VALUES (?,?,?)",(x.session_id,"user","Tool result: "+json.dumps(state["result"],ensure_ascii=False)))
    con.execute("INSERT INTO sessions(session_id,role,content) VALUES (?,?,?)",(x.session_id,"assistant",answer)); con.commit(); con.close()
    sql,result=state.get("sql",""),state.get("result",[]); steps=[{"name":"RAG metadata retrieval","detail":f"Retrieved {len(state.get('rag',[]))} metadata / metric / SQL-example chunks"},{"name":"LLM planning","detail":"Model chose chat or tool usage"}]
    if sql: steps += [{"name":"SQL guardrail","detail":"Read-only and catalog allowlist passed"},{"name":"Local Athena simulation","detail":f"Returned {len(result)} rows"}]
    return {"answer":answer,"intent":"DATA_QUERY" if sql else "CHAT","sql":sql,"result":result,"analysis":answer if state.get("final") else "","catalog":[{"name":item["title"]} for item in state.get("rag",[])],"modelMode":"configured","steps":steps}
