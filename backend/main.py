"""Configurable local Data Agent platform, powered by FastAPI and LangGraph."""
import json, os, re, sqlite3, uuid
from pathlib import Path
from typing import Any

import httpx
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".data"; DATA.mkdir(exist_ok=True)
PLATFORM_DB, DEMO_DB = DATA / "platform.db", DATA / "demo.db"
CATALOG = json.loads((ROOT / "metadata" / "catalog.json").read_text(encoding="utf-8"))
ALLOWED_TABLES = {item["name"] for item in CATALOG}

def connect(path: Path = PLATFORM_DB):
    con = sqlite3.connect(path); con.row_factory = sqlite3.Row; return con

def cipher():
    key = os.getenv("APP_ENCRYPTION_KEY")
    if not key: raise RuntimeError("APP_ENCRYPTION_KEY is required in .env")
    return Fernet(key.encode())

def init_storage():
    con = connect()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS providers (id TEXT PRIMARY KEY,name TEXT NOT NULL,kind TEXT NOT NULL,base_url TEXT NOT NULL,api_key TEXT NOT NULL,enabled INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS models (id TEXT PRIMARY KEY,provider_id TEXT NOT NULL,model_id TEXT NOT NULL,display_name TEXT NOT NULL,enabled INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL,model_id TEXT NOT NULL,tools TEXT NOT NULL,system_prompt TEXT NOT NULL,enabled INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions (session_id TEXT NOT NULL,role TEXT NOT NULL,content TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL,logs TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    if not con.execute("SELECT 1 FROM providers LIMIT 1").fetchone() and os.getenv("DEEPSEEK_API_KEY"):
        provider_id, model_id = str(uuid.uuid4()), str(uuid.uuid4())
        con.execute("INSERT INTO providers VALUES (?,?,?,?,?,?)", (provider_id,"DeepSeek","openai-compatible",os.getenv("DEEPSEEK_BASE_URL","https://api.deepseek.com"),cipher().encrypt(os.environ["DEEPSEEK_API_KEY"].encode()).decode(),1))
        con.execute("INSERT INTO models VALUES (?,?,?,?,?)", (model_id,provider_id,os.getenv("DEEPSEEK_MODEL","deepseek-v4-flash"),"DeepSeek V4 Flash",1))
        con.execute("INSERT INTO agents VALUES (?,?,?,?,?,?,?)", ("data-agent","Data Agent","面向本地电商数据湖的对话分析智能体",model_id,json.dumps(["catalog","query","etl"]),"你是严谨、友好的数据分析智能体。",1))
    con.commit(); con.close()
    demo = connect(DEMO_DB)
    demo.executescript("""
    CREATE TABLE IF NOT EXISTS users (user_id INTEGER,name TEXT,region TEXT,city TEXT);
    CREATE TABLE IF NOT EXISTS products (product_id INTEGER,name TEXT,category TEXT,unit_price REAL);
    CREATE TABLE IF NOT EXISTS orders (order_id INTEGER,order_date TEXT,user_id INTEGER,product_id INTEGER,quantity INTEGER,amount REAL,status TEXT);
    """)
    if not demo.execute("SELECT 1 FROM orders LIMIT 1").fetchone():
        demo.executescript("""
        INSERT INTO users VALUES (1,'王芳','华东','上海'),(2,'李明','华东','杭州'),(3,'张伟','华南','深圳');
        INSERT INTO products VALUES (101,'无线耳机','数码',499),(102,'保温杯','家居',129),(103,'跑鞋','运动',699);
        INSERT INTO orders VALUES (1,'2026-07-03',1,101,1,499,'PAID'),(2,'2026-07-09',2,102,2,258,'PAID'),(3,'2026-07-21',1,103,1,699,'PAID'),(4,'2026-07-27',2,101,1,499,'PAID');
        """)
    demo.commit(); demo.close()

class ProviderIn(BaseModel):
    name: str; kind: str = "openai-compatible"; base_url: str; api_key: str = ""; enabled: bool = True
class ModelIn(BaseModel):
    provider_id: str; model_id: str; display_name: str; enabled: bool = True
class AgentIn(BaseModel):
    name: str; description: str = ""; model_id: str
    tools: list[str] = Field(default_factory=lambda: ["catalog","query","etl"])
    system_prompt: str = "你是严谨、友好的数据分析智能体。"; enabled: bool = True
class ChatIn(BaseModel):
    message: str = Field(min_length=1,max_length=4000); session_id: str = "default"; agent_id: str = "data-agent"

def public_provider(row):
    return {"id":row["id"],"name":row["name"],"kind":row["kind"],"base_url":row["base_url"],"enabled":bool(row["enabled"]),"api_key_mask":"••••••••" if row["api_key"] else ""}

def catalog_context(question: str):
    tokens=set(re.findall(r"[a-zA-Z_]+|[\u4e00-\u9fff]{2,}",question.lower())); matches=[]
    for item in CATALOG:
        score=sum(token in json.dumps(item,ensure_ascii=False).lower() for token in tokens)
        if score: matches.append((score,item))
    return [item for _,item in sorted(matches,key=lambda pair:pair[0],reverse=True)[:3]] or CATALOG[:2]

def query_guard(sql: str):
    sql=sql.strip().rstrip(";"); low=sql.lower()
    if not low.startswith(("select","with")) or ";" in sql: raise HTTPException(400,"安全策略拒绝：仅允许单条只读 SELECT / WITH 查询")
    if re.search(r"\b(drop|delete|insert|update|alter|attach|pragma|vacuum|create|replace)\b",low): raise HTTPException(400,"安全策略拒绝：检测到写入或管理型 SQL")
    tables=set(re.findall(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)",low))
    if not tables or not tables.issubset(ALLOWED_TABLES): raise HTTPException(400,"安全策略拒绝：只能查询已登记的数据目录表")
    return sql

async def call_model(agent_id, message, history):
    con=connect(); row=con.execute("SELECT a.system_prompt,m.model_id,p.base_url,p.api_key FROM agents a JOIN models m ON a.model_id=m.id JOIN providers p ON m.provider_id=p.id WHERE a.id=? AND a.enabled=1 AND m.enabled=1 AND p.enabled=1",(agent_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"未找到已启用的智能体或模型配置")
    if not row["api_key"]: raise HTTPException(400,"当前供应商未配置 API Key")
    relevant=catalog_context(message)
    system=f'''{row["system_prompt"]}
你可以自然闲聊，也可以协助本地电商数据分析。数据目录：{json.dumps(relevant,ensure_ascii=False)}。
根据用户意图自主决定是否使用数据查询。严格只生成已给出的表上的单条只读 SQLite SELECT/WITH。
只返回 JSON：{{"answer":"中文回答","sql":"可选 SQL 或空字符串","should_analyze":true/false}}。闲聊、模型身份、方法论或数据不足时 sql 必须为空。'''
    messages=[{"role":"system","content":system}]+[{"role":item["role"],"content":item["content"]} for item in history[-8:]]+[{"role":"user","content":message}]
    try:
        # Ignore a desktop SOCKS proxy inherited from the shell. Provider access
        # must use the configured Base URL directly and should not depend on a
        # machine-specific proxy extra being installed.
        async with httpx.AsyncClient(timeout=45, trust_env=False) as client:
            res=await client.post(row["base_url"].rstrip("/")+"/chat/completions",headers={"Authorization":f"Bearer {cipher().decrypt(row['api_key'].encode()).decode()}"},json={"model":row["model_id"],"messages":messages,"temperature":0.2,"response_format":{"type":"json_object"}})
        res.raise_for_status(); return json.loads(res.json()["choices"][0]["message"]["content"])
    except (httpx.HTTPError,KeyError,ValueError,json.JSONDecodeError) as exc: raise HTTPException(502,f"模型调用失败：{str(exc)[:160]}") from exc

async def plan_node(state): return {"plan":await call_model(state["agent_id"],state["message"],state["history"])}
def execute_node(state):
    sql=str(state["plan"].get("sql") or "").strip()
    if not sql: return {"sql":"","result":[]}
    con=connect(DEMO_DB)
    try: result=[dict(row) for row in con.execute(query_guard(sql)).fetchall()]
    except sqlite3.Error as exc: raise HTTPException(400,f"SQL 执行失败：{exc}") from exc
    finally: con.close()
    return {"sql":query_guard(sql),"result":result}
def route(state): return "execute" if str(state["plan"].get("sql") or "").strip() else "finish"
builder=StateGraph(dict); builder.add_node("plan",plan_node); builder.add_node("execute",execute_node); builder.add_edge(START,"plan"); builder.add_conditional_edges("plan",route,{"execute":"execute","finish":END}); builder.add_edge("execute",END); AGENT_GRAPH=builder.compile()

app=FastAPI(title="Local Data Agent Platform",version="1.0.0")
app.add_middleware(CORSMiddleware,allow_origins=["http://localhost:5173"],allow_methods=["*"],allow_headers=["*"])
@app.on_event("startup")
def startup(): init_storage()
@app.get("/api/health")
def health(): return {"status":"ok","runtime":"fastapi-langgraph"}
@app.get("/api/catalog")
def catalog(): return CATALOG
@app.get("/api/providers")
def list_providers():
    con=connect(); rows=[public_provider(row) for row in con.execute("SELECT * FROM providers")]; con.close(); return rows
@app.post("/api/providers")
def create_provider(payload:ProviderIn):
    ident=str(uuid.uuid4()); con=connect(); encrypted=cipher().encrypt(payload.api_key.encode()).decode() if payload.api_key else ""; con.execute("INSERT INTO providers VALUES (?,?,?,?,?,?)",(ident,payload.name,payload.kind,payload.base_url.rstrip("/"),encrypted,int(payload.enabled))); con.commit(); row=con.execute("SELECT * FROM providers WHERE id=?",(ident,)).fetchone(); con.close(); return public_provider(row)
@app.get("/api/models")
def list_models():
    con=connect(); rows=[{**dict(row),"enabled":bool(row["enabled"])} for row in con.execute("SELECT * FROM models")]; con.close(); return rows
@app.post("/api/models")
def create_model(payload:ModelIn):
    ident=str(uuid.uuid4()); con=connect()
    if not con.execute("SELECT 1 FROM providers WHERE id=?",(payload.provider_id,)).fetchone(): con.close(); raise HTTPException(404,"供应商不存在")
    con.execute("INSERT INTO models VALUES (?,?,?,?,?)",(ident,payload.provider_id,payload.model_id,payload.display_name,int(payload.enabled))); con.commit(); con.close(); return {"id":ident,**payload.model_dump()}
@app.get("/api/agents")
def list_agents():
    con=connect(); rows=[{**dict(row),"tools":json.loads(row["tools"]),"enabled":bool(row["enabled"])} for row in con.execute("SELECT * FROM agents")]; con.close(); return rows
@app.post("/api/agents")
def create_agent(payload:AgentIn):
    ident=str(uuid.uuid4()); con=connect()
    if not con.execute("SELECT 1 FROM models WHERE id=?",(payload.model_id,)).fetchone(): con.close(); raise HTTPException(404,"模型不存在")
    con.execute("INSERT INTO agents VALUES (?,?,?,?,?,?,?)",(ident,payload.name,payload.description,payload.model_id,json.dumps(payload.tools),payload.system_prompt,int(payload.enabled))); con.commit(); con.close(); return {"id":ident,**payload.model_dump()}
@app.get("/api/jobs/{job_id}")
def job(job_id:str):
    con=connect(); row=con.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"任务不存在")
    return {**dict(row),"logs":json.loads(row["logs"])}
@app.post("/api/chat")
async def chat(payload:ChatIn):
    con=connect(); history=con.execute("SELECT role,content FROM sessions WHERE session_id=? ORDER BY created_at",(payload.session_id,)).fetchall(); con.close()
    state=await AGENT_GRAPH.ainvoke({"message":payload.message,"agent_id":payload.agent_id,"history":history}); plan=state["plan"]; sql,result=state.get("sql",""),state.get("result",[]); answer=str(plan.get("answer") or "我已处理你的请求。")
    con=connect(); con.execute("INSERT INTO sessions(session_id,role,content) VALUES (?,?,?)",(payload.session_id,"user",payload.message)); con.execute("INSERT INTO sessions(session_id,role,content) VALUES (?,?,?)",(payload.session_id,"assistant",answer)); con.commit(); con.close()
    steps=[{"name":"LLM 意图规划","detail":"由当前智能体绑定的可配置模型完成"}]
    if sql: steps += [{"name":"SQL 安全审核","detail":"通过：只读 SQL、目录白名单"},{"name":"本地 Athena 模拟执行","detail":f"返回 {len(result)} 行数据"}]
    return {"answer":answer,"intent":"DATA_QUERY" if sql else "CHAT","sql":sql,"result":result,"analysis":answer if plan.get("should_analyze") else "","catalog":[{"name":item["name"]} for item in catalog_context(payload.message)],"modelMode":"configured","steps":steps}
