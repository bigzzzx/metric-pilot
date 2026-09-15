"""HTTP routes only. Agent, RAG, model adapter, and tools live in separate modules."""
from contextlib import asynccontextmanager
import json
import os
import uuid
import httpx

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.agent import run_agent, run_agent_stream
from backend.api import admin_router, catalog_router, knowledge_router, metadata_sync_router, quality_router, tools_router
from backend.catalog import init_catalog_schema
from backend.catalog.repository import provision_user_access
from backend.catalog.publications import require_catalog_admin, require_platform_admin
from backend.catalog.sync import record_discovery
from backend.catalog.service import is_platform_admin
from backend.connectors import ConnectorUnavailable
from backend.data_sources.service import (
    DataSourceInUse,
    DataSourceProtected,
    connector_from_row,
    delete_source,
    encrypted_password,
    source_view,
)
from backend.conversations import (
    begin_turn,
    completed_response,
    ConversationConflictError,
    context_for_agent,
    create_conversation,
    ensure_conversation,
    fail_turn,
    finish_turn,
    get_messages,
    init_conversation_storage,
    list_conversations,
)
from backend.core import db, secrets
from backend.knowledge.repository import init_knowledge_schema
from backend.http_limits import UploadBodyLimitMiddleware
from backend.extensions import create_mcp_server, create_skill, list_mcp_servers, list_skills, tools as extension_tools
from backend.mcp.client import McpUnavailable, discover_server
from backend.mcp.repository import (
    create_server, delete_server, init_mcp_schema, list_tools as list_mcp_tools,
    record_server_status, replace_discovered_tools, update_server, update_tool,
)
from backend.mcp.runtime import register_mcp_tools
from backend.skills.repository import (
    create_skill as create_managed_skill, delete_skill, init_skill_schema, list_versions,
    update_skill,
)
from backend.auth import SESSION_COOKIE, create_session, current_user, hash_password, require_user, revoke_session, verify_password
from backend.model_client import ModelOutputError, ModelProviderError
from backend.memory import compact_conversation_if_needed, remember_explicit_request
from backend.memory_store import apply_memory_actions
from backend.memory_vector_sync import synchronize_forgotten_memory_vectors
from backend.memory_migrations import backfill_explicit_memories_once
from backend.observability import configure_logging, finish_request, log_event, log_exception, start_request
from backend.service_health import dependency_health
from backend.provider_transport import client_options, normalize_connection
from backend.query_trace import build_query_trace
from backend.schemas import ChatResponse
from backend.tools.approvals import init_tool_runtime_schema
from backend.tools.registry import initialized_registry
from backend.turn_cache import load_cached_response, response_cache_identity, store_cached_response
from backend.summary_background import summarize_after_response
from backend.api.attachments import router as attachments_router
from backend.attachments import repository as attachment_repository


def init_storage(connection_factory=None):
    """Initialize all platform storage through one injectable connection source."""
    connection_factory = connection_factory or db
    init_knowledge_schema(connection_factory)
    init_tool_runtime_schema(connection_factory)
    con = connection_factory()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS providers(id TEXT PRIMARY KEY,name TEXT,kind TEXT,base_url TEXT,api_key TEXT,enabled INTEGER,connection_mode TEXT NOT NULL DEFAULT 'direct',proxy_url TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY,provider_id TEXT,model_id TEXT,display_name TEXT,enabled INTEGER);
    CREATE TABLE IF NOT EXISTS agents(id TEXT PRIMARY KEY,name TEXT,description TEXT,model_id TEXT,tools TEXT,system_prompt TEXT,enabled INTEGER);
    CREATE TABLE IF NOT EXISTS sessions(session_id TEXT,role TEXT,content TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,is_active INTEGER NOT NULL DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS auth_sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires_at TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS data_sources(id TEXT PRIMARY KEY,name TEXT,kind TEXT,endpoint TEXT,catalog_name TEXT,auth_type TEXT,credential TEXT,enabled INTEGER,last_synced_at TEXT,username TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS skills(id TEXT PRIMARY KEY,name TEXT NOT NULL,description TEXT DEFAULT '',instructions TEXT NOT NULL,enabled INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS mcp_servers(id TEXT PRIMARY KEY,name TEXT NOT NULL,transport TEXT NOT NULL,endpoint TEXT NOT NULL,api_key TEXT DEFAULT '',enabled INTEGER DEFAULT 1,last_tested_at TEXT,last_test_status TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    init_mcp_schema(con)
    init_skill_schema(con)
    columns = {row["name"] for row in con.execute("PRAGMA table_info(agents)")}
    if "mcp_servers" not in columns: con.execute("ALTER TABLE agents ADD COLUMN mcp_servers TEXT DEFAULT '[]'")
    if "skills" not in columns: con.execute("ALTER TABLE agents ADD COLUMN skills TEXT DEFAULT '[]'")
    session_columns = {row["name"] for row in con.execute("PRAGMA table_info(sessions)")}
    if "user_id" not in session_columns: con.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT DEFAULT ''")
    user_columns = {row["name"] for row in con.execute("PRAGMA table_info(users)")}
    if "is_active" not in user_columns: con.execute("ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    provider_columns = {row["name"] for row in con.execute("PRAGMA table_info(providers)")}
    if "connection_mode" not in provider_columns: con.execute("ALTER TABLE providers ADD COLUMN connection_mode TEXT NOT NULL DEFAULT 'direct'")
    if "proxy_url" not in provider_columns: con.execute("ALTER TABLE providers ADD COLUMN proxy_url TEXT NOT NULL DEFAULT ''")
    source_columns = {row["name"] for row in con.execute("PRAGMA table_info(data_sources)")}
    if "username" not in source_columns: con.execute("ALTER TABLE data_sources ADD COLUMN username TEXT NOT NULL DEFAULT ''")
    if not con.execute("SELECT 1 FROM providers").fetchone() and os.getenv("DEEPSEEK_API_KEY"):
        provider_id, model_id = str(uuid.uuid4()), str(uuid.uuid4())
        con.execute("INSERT INTO providers(id,name,kind,base_url,api_key,enabled,connection_mode,proxy_url) VALUES (?,?,?,?,?,1,'direct','')", (provider_id,"DeepSeek","openai-compatible",os.getenv("DEEPSEEK_BASE_URL","https://api.deepseek.com"),secrets().encrypt(os.environ["DEEPSEEK_API_KEY"].encode()).decode()))
        con.execute("INSERT INTO models VALUES (?,?,?,?,1)", (model_id,provider_id,os.getenv("DEEPSEEK_MODEL","deepseek-v4-flash"),"DeepSeek V4 Flash"))
    default_model = con.execute("SELECT id FROM models WHERE enabled=1 LIMIT 1").fetchone()
    if default_model:
        configured_agents = [
            ("general-agent", "General Agent", "回答通用问题，不调用数据工具。", ["chat", "memory_search", "memory_write", "memory_forget"], "你是一个友好、可靠的通用中文助手。回答日常问题时不要声称查询了业务数据。"),
            ("data-query-agent", "Data Query Agent", "检索数据资产知识并按需生成安全 SQL。", ["catalog_rag", "readonly_sql", "memory_search", "memory_write", "memory_forget"], "你是严谨的数据查询智能体。只能依据 RAG 元数据和允许的工具处理数据问题。"),
            ("data-analysis-agent", "Data Analysis Agent", "基于可信查询结果生成业务分析。", ["catalog_rag", "create_visualization", "memory_search", "memory_write", "memory_forget"], "你是业务数据分析师。只能依据可信查询结果和指标口径给出结论、限制与后续分析建议。"),
        ]
        for agent_id, name, description, tools, prompt in configured_agents:
            con.execute("INSERT OR IGNORE INTO agents(id,name,description,model_id,tools,system_prompt,enabled) VALUES (?,?,?,?,?,?,1)", (agent_id, name, description, default_model["id"], json.dumps(tools), prompt))
    # Upgrade existing agent configurations once so users can later disable the
    # memory tools explicitly in the management UI without startup re-enabling them.
    con.execute("CREATE TABLE IF NOT EXISTS app_migrations(version TEXT PRIMARY KEY,applied_at TEXT NOT NULL)")
    memory_tools_version = "003_namespaced_memory_tools"
    if not con.execute("SELECT 1 FROM app_migrations WHERE version=?", (memory_tools_version,)).fetchone():
        for row in con.execute("SELECT id,tools FROM agents").fetchall():
            configured = json.loads(row["tools"] or "[]")
            for tool_id in ("memory_write", "memory_forget"):
                if tool_id not in configured:
                    configured.append(tool_id)
            con.execute("UPDATE agents SET tools=? WHERE id=?", (json.dumps(configured), row["id"]))
        con.execute("INSERT INTO app_migrations(version,applied_at) VALUES (?,CURRENT_TIMESTAMP)", (memory_tools_version,))
    memory_search_version = "004_conditional_memory_search"
    if not con.execute("SELECT 1 FROM app_migrations WHERE version=?", (memory_search_version,)).fetchone():
        for row in con.execute("SELECT id,tools FROM agents").fetchall():
            configured = json.loads(row["tools"] or "[]")
            if "memory_search" not in configured:
                configured.append("memory_search")
            con.execute("UPDATE agents SET tools=? WHERE id=?", (json.dumps(configured), row["id"]))
        con.execute("INSERT INTO app_migrations(version,applied_at) VALUES (?,CURRENT_TIMESTAMP)", (memory_search_version,))    # Local OpenAI-compatible runtimes are visible in provider management but
    tool_registry_version = "005_governed_tool_registry"
    if not con.execute("SELECT 1 FROM app_migrations WHERE version=?", (tool_registry_version,)).fetchone():
        for row in con.execute("SELECT id,tools FROM agents").fetchall():
            configured = [item for item in json.loads(row["tools"] or "[]") if item not in {"chat", "query_result_analysis", "etl"}]
            if row["id"] == "data-query-agent":
                for tool_id in ("sql_write", "etl_execute"):
                    if tool_id not in configured:
                        configured.append(tool_id)
            con.execute("UPDATE agents SET tools=? WHERE id=?", (json.dumps(configured), row["id"]))
        con.execute("INSERT INTO app_migrations(version,applied_at) VALUES (?,CURRENT_TIMESTAMP)", (tool_registry_version,))
    from backend.query_quality.schema_lookup import migrate_schema_binding
    migrate_schema_binding(con)
    # Local OpenAI-compatible runtimes are visible in provider management but
    # disabled until a user starts/configures one.
    for provider_id, name, kind, url in [
        ("local-ollama", "Ollama (Local)", "ollama", "http://localhost:11434/v1"),
        ("local-vllm", "vLLM (Local)", "vllm", "http://localhost:8000/v1"),
    ]:
        con.execute("INSERT OR IGNORE INTO providers(id,name,kind,base_url,api_key,enabled,connection_mode,proxy_url) VALUES (?,?,?,?,?,0,'direct','')", (provider_id, name, kind, url, ""))
    # The pre-LangGraph single-agent configuration is obsolete. Runtime nodes
    # use the three role-specific configurations above.
    con.execute("DELETE FROM agents WHERE id='data-agent'")
    con.execute("""INSERT OR IGNORE INTO data_sources
      (id,name,kind,endpoint,catalog_name,auth_type,credential,enabled,last_synced_at,username)
      VALUES (?,?,?,?,?,?,?,?,?,?)""", ("local-ecommerce-demo", "本地电商演示数仓", "local-sqlite", "local://demo.db", "ecommerce", "none", "", 1, None, ""))
    con.execute("INSERT OR IGNORE INTO knowledge_bases(id,name,description) VALUES (?,?,?)", ("business-knowledge", "业务知识库", "存放指标口径、业务别名、数据使用说明等可检索知识。"))
    con.commit(); con.close()
    init_catalog_schema(connection_factory)
    attachment_repository.init_schema(connection_factory)
    from backend.visualization.node import init_bindings
    init_bindings(connection_factory)


class ProviderIn(BaseModel):
    name: str; base_url: str; api_key: str = ""; kind: str = "openai-compatible"; enabled: bool = True; connection_mode: str = "direct"; proxy_url: str = ""
class ModelIn(BaseModel):
    provider_id: str; model_id: str; display_name: str; enabled: bool = True
class ProviderUpdate(BaseModel):
    name: str; kind: str; base_url: str; api_key: str = ""; enabled: bool = True; connection_mode: str = "direct"; proxy_url: str = ""
class AgentUpdate(BaseModel):
    model_id: str; tools: list[str]; mcp_servers: list[str] = []; mcp_tools: list[str] = []; skills: list[str] = []; system_prompt: str; enabled: bool = True
class ChatIn(BaseModel):
    message: str = Field(min_length=1)
    attachment_ids: list[str] = Field(default_factory=list, max_length=5)
    session_id: str = "default"
    model_id: str | None = None
    client_message_id: str | None = Field(default=None, max_length=100)
class CredentialsIn(BaseModel):
    username: str = Field(min_length=3, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")
    password: str = Field(min_length=8, max_length=128)
class DataSourceIn(BaseModel):
    name: str = Field(min_length=1)
    kind: str
    endpoint: str = ""
    catalog_name: str = ""
    auth_type: str = "read_only_account"
    username: str = Field(default="", max_length=128)
    credential: str = ""
    enabled: bool = True
class DataSourceUpdate(DataSourceIn):
    pass
class SkillIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)
    instructions: str = Field(min_length=1, max_length=20_000)
    applicability: str = Field(default="", max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=20)
    examples: list[str] = Field(default_factory=list, max_length=20)
    priority: int = Field(default=50, ge=0, le=100)
    always_on: bool = False
    enabled: bool = True
    archived: bool = False
    change_note: str = Field(default="", max_length=300)
class McpServerIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    transport: str = "streamable-http"
    endpoint: str = Field(min_length=1, max_length=500)
    auth_type: str = "bearer"
    api_key: str = ""
    timeout_seconds: int = Field(default=30, ge=3, le=300)
    enabled: bool = True
class McpToolUpdate(BaseModel):
    enabled: bool = True
    risk_level: str = Field(pattern="^(low|medium|high)$")
    required_permission: str = Field(default="", max_length=100)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configure_logging()
    init_storage()
    init_conversation_storage()
    backfill_explicit_memories_once(db)
    register_mcp_tools(initialized_registry())
    yield


app = FastAPI(title="Local Data Agent Platform", lifespan=lifespan)
app.include_router(attachments_router)
app.add_middleware(UploadBodyLimitMiddleware, max_request_bytes=21 * 1024 * 1024, path_suffix='/attachments/upload')
app.add_middleware(
    UploadBodyLimitMiddleware,
    max_request_bytes=21 * 1024 * 1024,
    path_suffix="/documents/upload",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
app.include_router(knowledge_router)
app.include_router(catalog_router)
app.include_router(admin_router)
app.include_router(tools_router)
app.include_router(metadata_sync_router)
app.include_router(quality_router)


@app.exception_handler(Exception)
async def unexpected_error_handler(_request: Request, exc: Exception):
    """Never let a plain-text 500 reach the browser."""
    log_exception("api.unhandled_error", error_type=type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "The service encountered an unexpected error. Please retry.", "code": "internal_error"})


@app.middleware("http")
async def request_trace(request: Request, call_next):
    request_id, token = start_request(request.headers.get("X-Request-ID"))
    request.state.request_id = request_id
    try:
        response = await call_next(request)
        log_event("api.request_complete", method=request.method, path=request.url.path, status=response.status_code)
    except Exception as exc:
        log_exception("api.unhandled_error", method=request.method, path=request.url.path, error_type=type(exc).__name__)
        response = JSONResponse(status_code=500, content={"detail": "The service encountered an unexpected error. Please retry.", "code": "internal_error"})
    response.headers["X-Request-ID"] = request_id
    finish_request(token)
    return response


@app.get("/api/health")
def health(response: Response):
    result = dependency_health()
    if result["status"] != "ok":
        response.status_code = 503
    return result


@app.get("/api/health/live")
def liveness():
    """A dependency-free process check for supervisors restarting the API itself."""
    return {"status": "ok", "runtime": "fastapi-langgraph"}


def public_user(user: dict) -> dict:
    """Return the smallest client-safe identity and its catalog capability."""
    return {
        "id": user["id"],
        "username": user["username"],
        "is_catalog_admin": is_platform_admin(user["id"]),
    }


def auth_response(response: Response, user: dict) -> dict:
    token = create_session(user["id"])
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=7 * 24 * 3600, secure=False)
    return public_user(user)


@app.post("/api/auth/register")
def register(payload: CredentialsIn, response: Response):
    con = db()
    if con.execute("SELECT 1 FROM users WHERE username=?", (payload.username,)).fetchone():
        con.close(); raise HTTPException(409, "Username is already registered")
    user = {"id": str(uuid.uuid4()), "username": payload.username}
    con.execute("INSERT INTO users(id,username,password_hash) VALUES (?,?,?)", (user["id"], user["username"], hash_password(payload.password)))
    con.execute("INSERT INTO knowledge_bases(id,name,description,owner_id) VALUES (?,?,?,?)", (str(uuid.uuid4()), "我的业务知识库", "仅自己可见的业务说明、指标口径与分析规则。", user["id"]))
    con.commit(); con.close()
    provision_user_access(user["id"])
    return auth_response(response, user)


@app.post("/api/auth/login")
def login(payload: CredentialsIn, response: Response):
    con = db(); row = con.execute("SELECT id,username,password_hash,is_active FROM users WHERE username=?", (payload.username,)).fetchone(); con.close()
    if not row or not verify_password(payload.password, row["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    if not row["is_active"]:
        raise HTTPException(403, "This account has been disabled")
    return auth_response(response, dict(row))


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    revoke_session(request.cookies.get(SESSION_COOKIE)); response.delete_cookie(SESSION_COOKIE); return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    user = current_user(request)
    if not user: raise HTTPException(401, "Authentication required")
    return public_user(user)


@app.post("/api/conversations")
def add_conversation(user: dict = Depends(require_user)):
    return create_conversation(user["id"])


@app.get("/api/conversations")
def conversations(user: dict = Depends(require_user)):
    return list_conversations(user["id"])


@app.get("/api/conversations/{conversation_id}/messages")
def conversation_messages(conversation_id: str, user: dict = Depends(require_user)):
    try:
        return get_messages(conversation_id, user["id"])
    except LookupError as exc:
        raise HTTPException(404, "Conversation not found") from exc


@app.get("/api/tools")
def tools_catalog(_user: dict = Depends(require_user)):
    return extension_tools()


@app.get("/api/skills")
def skills(_user: dict = Depends(require_user)):
    return list_skills()


@app.post("/api/skills")
def add_skill(payload: SkillIn, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    return create_managed_skill(**payload.model_dump(exclude={"archived"}))


@app.put("/api/skills/{skill_id}")
def edit_skill(skill_id: str, payload: SkillIn, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    result = update_skill(skill_id, payload.model_dump())
    if not result: raise HTTPException(404, "Skill not found")
    return result


@app.delete("/api/skills/{skill_id}")
def remove_skill(skill_id: str, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    result = delete_skill(skill_id)
    if not result:
        raise HTTPException(404, "Skill not found")
    return result


@app.get("/api/skills/{skill_id}/versions")
def skill_versions(skill_id: str, _user: dict = Depends(require_user)):
    return list_versions(skill_id)


@app.get("/api/mcp-servers")
def mcp_servers(_user: dict = Depends(require_user)):
    return list_mcp_servers()


@app.post("/api/mcp-servers")
def add_mcp_server(payload: McpServerIn, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    return create_server(**payload.model_dump())


@app.put("/api/mcp-servers/{server_id}")
def edit_mcp_server(server_id: str, payload: McpServerIn, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    result = update_server(server_id, payload.model_dump())
    if not result: raise HTTPException(404, "MCP Server not found")
    return result


@app.delete("/api/mcp-servers/{server_id}")
def remove_mcp_server(server_id: str, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    result = delete_server(server_id)
    if not result:
        raise HTTPException(404, "MCP Server not found")
    registry = initialized_registry()
    for tool_id in result["tool_ids"]:
        registry.remove(tool_id)
    return result


@app.get("/api/mcp-servers/{server_id}/tools")
def mcp_server_tools(server_id: str, _user: dict = Depends(require_user)):
    return list_mcp_tools(server_id)


@app.put("/api/mcp-tools/{tool_id:path}")
def edit_mcp_tool(tool_id: str, payload: McpToolUpdate, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    result = update_tool(tool_id, payload.model_dump())
    if not result: raise HTTPException(404, "MCP tool not found")
    register_mcp_tools(initialized_registry())
    return result


@app.post("/api/mcp-servers/{server_id}/test")
async def test_mcp_server(server_id: str, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    try:
        discovered = await discover_server(server_id)
        status = f"connected ({len(discovered['tools'])} tools)"
        record_server_status(server_id, status, protocol_version=discovered["protocol_version"], capabilities=discovered["capabilities"])
        return {"status": status, "tool_count": len(discovered["tools"]), "server_info": discovered["server_info"]}
    except McpUnavailable as exc:
        status = f"failed: {str(exc)[:200]}"; record_server_status(server_id, status)
        return JSONResponse(status_code=502, content={"detail": status, "code": "mcp_test_failed"})


@app.post("/api/mcp-servers/{server_id}/sync")
async def sync_mcp_server(server_id: str, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    try:
        discovered = await discover_server(server_id)
        synced = replace_discovered_tools(server_id, discovered["tools"])
        status = f"connected ({len(synced)} tools)"
        record_server_status(server_id, status, protocol_version=discovered["protocol_version"], capabilities=discovered["capabilities"], synced=True)
        register_mcp_tools(initialized_registry())
        return {"status": status, "tool_count": len(synced), "tools": synced}
    except McpUnavailable as exc:
        status = f"failed: {str(exc)[:200]}"; record_server_status(server_id, status)
        return JSONResponse(status_code=502, content={"detail": status, "code": "mcp_sync_failed"})


def data_source_view(row) -> dict:
    return source_view(row)


@app.get("/api/data-sources")
def data_sources(user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    con = db(); rows = [data_source_view(row) for row in con.execute("SELECT * FROM data_sources ORDER BY id")]; con.close(); return rows


@app.post("/api/data-sources")
def create_data_source(payload: DataSourceIn, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    source_id = str(uuid.uuid4())
    credential = encrypted_password(payload.credential)
    con = db(); con.execute("""INSERT INTO data_sources
      (id,name,kind,endpoint,catalog_name,auth_type,credential,enabled,last_synced_at,username)
      VALUES (?,?,?,?,?,?,?,?,?,?)""", (source_id, payload.name, payload.kind, payload.endpoint.rstrip("/"), payload.catalog_name, payload.auth_type, credential, int(payload.enabled), None, payload.username)); con.commit()
    row = con.execute("SELECT * FROM data_sources WHERE id=?", (source_id,)).fetchone(); con.close(); return data_source_view(row)


@app.put("/api/data-sources/{source_id}")
def update_data_source(source_id: str, payload: DataSourceUpdate, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    con = db(); existing = con.execute("SELECT * FROM data_sources WHERE id=?", (source_id,)).fetchone()
    if not existing:
        con.close(); raise HTTPException(404, "Data source not found")
    credential = encrypted_password(payload.credential, existing["credential"])
    con.execute("UPDATE data_sources SET name=?,kind=?,endpoint=?,catalog_name=?,auth_type=?,credential=?,enabled=?,username=? WHERE id=?", (payload.name, payload.kind, payload.endpoint.rstrip("/"), payload.catalog_name, payload.auth_type, credential, int(payload.enabled), payload.username, source_id)); con.commit()
    row = con.execute("SELECT * FROM data_sources WHERE id=?", (source_id,)).fetchone(); con.close(); return data_source_view(row)


@app.delete("/api/data-sources/{source_id}")
def remove_data_source(source_id: str, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    try:
        return delete_source(source_id, user["id"])
    except LookupError as exc:
        raise HTTPException(404, "Data source not found") from exc
    except DataSourceProtected as exc:
        raise HTTPException(409, str(exc)) from exc
    except DataSourceInUse as exc:
        raise HTTPException(409, str(exc)) from exc


def source_connector(source_id: str):
    con = db(); row = con.execute("SELECT * FROM data_sources WHERE id=? AND enabled=1", (source_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404, "Enabled data source not found")
    return row, connector_from_row(row)


@app.post("/api/data-sources/{source_id}/test")
def test_data_source(source_id: str, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    try:
        row, connector = source_connector(source_id)
        result = connector.test()
        log_event("catalog.source_test", source_id=source_id, kind=row["kind"], status=result["status"])
        return result
    except ConnectorUnavailable as exc:
        return JSONResponse(status_code=501, content={"detail": str(exc), "code": "connector_unavailable"})


@app.post("/api/data-sources/{source_id}/sync")
def sync_data_source(source_id: str, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    try:
        row, connector = source_connector(source_id)
        result = connector.discover()
        log_event("catalog.source_sync", source_id=source_id, kind=row["kind"], table_count=result["table_count"])
        review = record_discovery(source_id, result, user["id"])
        con = db(); con.execute("UPDATE data_sources SET last_synced_at=CURRENT_TIMESTAMP WHERE id=?", (source_id,)); con.commit(); con.close()
        return {**result, **review}
    except ConnectorUnavailable as exc:
        return JSONResponse(status_code=501, content={"detail": str(exc), "code": "connector_unavailable"})
@app.get("/api/providers")
def providers():
    con=db(); rows=[{"id":r["id"],"name":r["name"],"kind":r["kind"],"base_url":r["base_url"],"enabled":bool(r["enabled"]),"connection_mode":r["connection_mode"] or "direct","proxy_url":r["proxy_url"] or "","api_key_mask":"••••••••" if r["api_key"] else ""} for r in con.execute("SELECT * FROM providers")]; con.close(); return rows


def provider_connection_or_422(connection_mode: str, proxy_url: str) -> tuple[str, str]:
    try:
        return normalize_connection(connection_mode, proxy_url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/providers")
def provider(x:ProviderIn):
    mode, proxy_url = provider_connection_or_422(x.connection_mode, x.proxy_url)
    ident=str(uuid.uuid4()); con=db(); con.execute("INSERT INTO providers(id,name,kind,base_url,api_key,enabled,connection_mode,proxy_url) VALUES (?,?,?,?,?,?,?,?)",(ident,x.name,x.kind,x.base_url.rstrip("/"),secrets().encrypt(x.api_key.encode()).decode() if x.api_key else "",int(x.enabled),mode,proxy_url)); con.commit(); con.close(); return {"id":ident,**x.model_dump(exclude={"api_key","connection_mode","proxy_url"}),"connection_mode":mode,"proxy_url":proxy_url}
@app.put("/api/providers/{provider_id}")
def update_provider(provider_id: str, payload: ProviderUpdate):
    mode, proxy_url = provider_connection_or_422(payload.connection_mode, payload.proxy_url)
    con=db(); old=con.execute("SELECT api_key FROM providers WHERE id=?", (provider_id,)).fetchone()
    if not old: con.close(); raise HTTPException(404, "Provider not found")
    encrypted=secrets().encrypt(payload.api_key.encode()).decode() if payload.api_key else old["api_key"]
    con.execute("UPDATE providers SET name=?,kind=?,base_url=?,api_key=?,enabled=?,connection_mode=?,proxy_url=? WHERE id=?", (payload.name,payload.kind,payload.base_url.rstrip("/"),encrypted,int(payload.enabled),mode,proxy_url,provider_id)); con.commit(); con.close(); return {"id":provider_id, **payload.model_dump(exclude={"api_key","connection_mode","proxy_url"}),"connection_mode":mode,"proxy_url":proxy_url}
@app.get("/api/providers/{provider_id}/models/discover")
async def discover_provider_models(provider_id: str):
    con=db(); row=con.execute("SELECT * FROM providers WHERE id=?",(provider_id,)).fetchone(); con.close()
    if not row: raise HTTPException(404,"Provider not found")
    if row["kind"] == "anthropic-messages": return {"models":[],"message":"Anthropic model discovery is not exposed by this demo; add a model ID manually."}
    key=secrets().decrypt(row["api_key"].encode()).decode() if row["api_key"] else ""
    try:
        async with httpx.AsyncClient(timeout=12, **client_options(dict(row))) as client:
            if row["kind"] == "ollama":
                url=row["base_url"].replace("/v1","").rstrip("/")+"/api/tags"; res=await client.get(url); data=res.json(); ids=[item["name"] for item in data.get("models",[])]
            else:
                res=await client.get(row["base_url"].rstrip("/")+"/models",headers={"Authorization":f"Bearer {key}"} if key else {}); data=res.json(); ids=[item["id"] for item in data.get("data",[])]
        res.raise_for_status(); return {"models":ids}
    except (httpx.HTTPError,KeyError,ValueError) as exc: return {"models":[],"message":f"Could not discover models: {str(exc)[:100]}"}
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
def agents(user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    con=db(); rows=[{**dict(r),"tools":json.loads(r["tools"]),"mcp_servers":json.loads(r["mcp_servers"] or "[]"),"mcp_tools":json.loads(r["mcp_tools"] or "[]"),"skills":json.loads(r["skills"] or "[]"),"enabled":bool(r["enabled"])} for r in con.execute("SELECT * FROM agents")]; con.close(); return rows
@app.put("/api/agents/{agent_id}")
def update_agent(agent_id: str, payload: AgentUpdate, user: dict = Depends(require_user)):
    require_platform_admin(user["id"])
    con=db()
    if not con.execute("SELECT 1 FROM agents WHERE id=?", (agent_id,)).fetchone():
        con.close(); raise HTTPException(404, "Agent not found")
    if not con.execute("SELECT 1 FROM models WHERE id=?", (payload.model_id,)).fetchone():
        con.close(); raise HTTPException(404, "Model not found")
    registered = {spec.id: spec for spec in initialized_registry().all()}
    unknown = sorted(set(payload.tools) - registered.keys())
    incompatible = sorted(
        tool_id for tool_id in payload.tools
        if tool_id in registered and registered[tool_id].allowed_agents
        and agent_id not in registered[tool_id].allowed_agents
    )
    if unknown or incompatible:
        con.close()
        details = []
        if unknown:
            details.append("未注册工具：" + "、".join(unknown))
        if incompatible:
            details.append("当前智能体不可绑定：" + "、".join(incompatible))
        raise HTTPException(422, "；".join(details))
    valid_servers = {row["id"] for row in con.execute("SELECT id FROM mcp_servers")}
    valid_mcp_tools = {row["id"] for row in con.execute("SELECT id FROM mcp_tools")}
    valid_skills = {row["id"] for row in con.execute("SELECT id FROM skills WHERE archived=0")}
    invalid_extensions = []
    if set(payload.mcp_servers) - valid_servers:
        invalid_extensions.append("未知 MCP Server：" + "、".join(sorted(set(payload.mcp_servers) - valid_servers)))
    if set(payload.mcp_tools) - valid_mcp_tools:
        invalid_extensions.append("未知 MCP 工具：" + "、".join(sorted(set(payload.mcp_tools) - valid_mcp_tools)))
    if set(payload.skills) - valid_skills:
        invalid_extensions.append("未知或已归档 Skill：" + "、".join(sorted(set(payload.skills) - valid_skills)))
    if invalid_extensions:
        con.close(); raise HTTPException(422, "；".join(invalid_extensions))
    con.execute("UPDATE agents SET model_id=?,tools=?,mcp_servers=?,mcp_tools=?,skills=?,system_prompt=?,enabled=? WHERE id=?", (payload.model_id, json.dumps(payload.tools), json.dumps(payload.mcp_servers), json.dumps(payload.mcp_tools), json.dumps(payload.skills), payload.system_prompt, int(payload.enabled), agent_id))
    con.commit(); con.close(); return {"id":agent_id, **payload.model_dump()}
def response_from_state(state: dict) -> ChatResponse:
    plan = state.get("plan", {})
    final = state.get("final", plan)
    answer = str(final.get("answer") or "I have processed your request.")
    sql, result = state.get("sql", ""), state.get("result", [])
    steps = [
        {"name": "RAG metadata retrieval", "detail": f"Retrieved {len(state.get('rag', []))} metadata / metric / SQL-example chunks"},
        {"name": "Query planning", "detail": "Governed metric compiler" if state.get('query_mode')=='text_to_metrics' else "Model chose chat or tool usage"},
    ]
    if sql:
        steps += [
            {"name": "SQL guardrail", "detail": "Query rejected or failed" if state.get('query_error') else "Read-only and catalog allowlist passed"},
            {"name": "Data source query", "detail": f"Source: {plan.get('source_id') or 'catalog-resolved'}; returned {len(result)} rows"},
        ]
    return ChatResponse(
        visualizations=state.get("visualizations", []),
        visualization_notice=state.get("visualization_notice", ""),
        answer=answer,
        source_id=plan.get('source_id'),
        intent="DATA_QUERY" if sql or state.get("visualizations") else "CHAT",
        sql=sql,
        result=result,
        analysis=answer if state.get("final") else "",
        catalog=[{"name": item["title"]} for item in state.get("rag", [])],
        steps=steps,
        approval=state.get("approval"),
        permission_error=state.get("permission_error"),
        tool_result=state.get("tool_result"),
        trace=build_query_trace(state),
    )


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def error_status(code: str) -> int:
    return {
        "model_provider_error": 502,
        "model_empty_content": 502,
        "invalid_model_output": 422,
        "internal_error": 500,
    }.get(code, 500)


def terminal_payload(message: dict) -> dict:
    payload = message.get("payload")
    return payload if isinstance(payload, dict) else {}


def cached_chat_response(message: dict):
    payload = terminal_payload(message)
    if message.get("kind") == "error":
        code = str(payload.get("code") or "internal_error")
        return JSONResponse(status_code=error_status(code), content=payload)
    return ChatResponse.model_validate(payload)


def persist_chat_error(
    conversation_id: str,
    user_id: str,
    client_message_id: str,
    detail: str,
    code: str,
    request_id: str,
) -> dict:
    payload = {"detail": detail, "code": code, "request_id": request_id}
    return fail_turn(
        conversation_id=conversation_id,
        user_id=user_id,
        client_message_id=client_message_id,
        error_payload=payload,
    )


async def refresh_memory_after_success(
    message: str,
    conversation_id: str,
    user_id: str,
    source_message_id: str | None = None,
    memory_actions: list[dict] | None = None,
    background_tasks: BackgroundTasks | None = None,
) -> None:
    """Persist only after a successful turn; failed model calls never create memories."""

    outcomes = apply_memory_actions(
        user_id,
        conversation_id,
        source_message_id,
        memory_actions or [],
    )
    synchronize_forgotten_memory_vectors(outcomes)
    # Explicit commands are deterministic fallback when the model did not
    # declare an action (for example a provider with weak structured output).
    if not outcomes:
        remember_explicit_request(message, user_id, conversation_id, source_message_id)
    if background_tasks is not None:
        background_tasks.add_task(summarize_after_response, conversation_id, user_id)
    else:
        await compact_conversation_if_needed(conversation_id, user_id)

@app.post("/api/chat", response_model=ChatResponse)
async def chat(x: ChatIn, request: Request, background_tasks: BackgroundTasks, user: dict = Depends(require_user)):
    client_message_id = x.client_message_id or str(uuid.uuid4())
    try:
        ensure_conversation(x.session_id, user["id"])
        turn = begin_turn(
            conversation_id=x.session_id,
            user_id=user["id"],
            content=x.message,
            client_message_id=client_message_id,
            attachment_ids=getattr(x, 'attachment_ids', []),
        )
        cached = completed_response(x.session_id, user["id"], client_message_id)
        if cached:
            if cached.get("kind") != "error":
                await refresh_memory_after_success(
                    x.message,
                    x.session_id,
                    user["id"],
                    turn["message"]["id"],
                    background_tasks=background_tasks,
                )
            return cached_chat_response(cached)
        context = context_for_agent(x.session_id, user["id"], exclude_client_message_id=client_message_id)
        context['attachments'] = attachment_repository.list_files(x.session_id, user['id'])
        turn_cache_key = response_cache_identity(
            x.message, user["id"], "data-query-agent", x.model_id,
            context=context,
        )
        cached_turn = load_cached_response(turn_cache_key)
        if cached_turn:
            stored = finish_turn(
                conversation_id=x.session_id,
                user_id=user["id"],
                client_message_id=client_message_id,
                response_payload=cached_turn,
                tool_result=cached_turn.get("result") if cached_turn.get("sql") else None,
            )
            await refresh_memory_after_success(
                x.message, x.session_id, user["id"], turn["message"]["id"],
                background_tasks=background_tasks,
            )
            return cached_chat_response(stored)
        state = await run_agent(x.message, "data-query-agent", context["history"], x.model_id, context["conversation_summary"], context["user_memories"], user["id"], x.session_id, turn["message"]["id"])
        response = response_from_state(state)
        store_cached_response(turn_cache_key, response, state)
        stored = finish_turn(
            conversation_id=x.session_id,
            user_id=user["id"],
            client_message_id=client_message_id,
            response_payload=response.model_dump(),
            tool_result=state.get("result") if state.get("sql") else None,
        )
        await refresh_memory_after_success(
            x.message,
            x.session_id,
            user["id"],
            turn["message"]["id"],
            state.get("memory_actions"),
            background_tasks=background_tasks,
        )
        return cached_chat_response(stored)
    except LookupError as exc:
        raise HTTPException(404, "Conversation not found") from exc
    except ConversationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ModelProviderError as exc:
        log_event("api.chat_error", level=30, code=exc.code)
        stored = persist_chat_error(x.session_id, user["id"], client_message_id, str(exc), exc.code, request.state.request_id)
        return cached_chat_response(stored)
    except ModelOutputError as exc:
        log_event("api.chat_error", level=30, code="invalid_model_output")
        stored = persist_chat_error(x.session_id, user["id"], client_message_id, str(exc), "invalid_model_output", request.state.request_id)
        return cached_chat_response(stored)
    except Exception as exc:
        log_exception("api.chat_error", code="internal_error", error_type=type(exc).__name__)
        detail = "The service encountered an unexpected error. Please retry."
        stored = persist_chat_error(x.session_id, user["id"], client_message_id, detail, "internal_error", request.state.request_id)
        return cached_chat_response(stored)


@app.post("/api/chat/stream")
async def chat_stream(x: ChatIn, request: Request, background_tasks: BackgroundTasks, user: dict = Depends(require_user)):
    request_id = request.state.request_id
    client_message_id = x.client_message_id or str(uuid.uuid4())
    try:
        ensure_conversation(x.session_id, user["id"])
        turn = begin_turn(
            conversation_id=x.session_id,
            user_id=user["id"],
            content=x.message,
            client_message_id=client_message_id,
            attachment_ids=getattr(x, 'attachment_ids', []),
        )
        cached = completed_response(x.session_id, user["id"], client_message_id)
        context = context_for_agent(
            x.session_id,
            user["id"],
            exclude_client_message_id=client_message_id,
        )
        context['attachments'] = attachment_repository.list_files(x.session_id, user['id'])
        turn_cache_key = response_cache_identity(
            x.message, user["id"], "data-query-agent", x.model_id,
            context=context,
        )
        cached_turn = load_cached_response(turn_cache_key)
    except LookupError as exc:
        raise HTTPException(404, "Conversation not found") from exc
    except ConversationConflictError as exc:
        raise HTTPException(409, str(exc)) from exc

    async def events():
        _, token = start_request(request_id)
        try:
            if cached:
                if cached.get("kind") != "error":
                    await refresh_memory_after_success(
                        x.message,
                        x.session_id,
                        user["id"],
                        turn["message"]["id"],
                        background_tasks=background_tasks,
                    )
                payload = terminal_payload(cached)
                yield sse("error" if cached.get("kind") == "error" else "done", payload)
                return
            if cached_turn:
                stored = finish_turn(
                    conversation_id=x.session_id,
                    user_id=user["id"],
                    client_message_id=client_message_id,
                    response_payload=cached_turn,
                    tool_result=cached_turn.get("result") if cached_turn.get("sql") else None,
                )
                await refresh_memory_after_success(
                    x.message, x.session_id, user["id"], turn["message"]["id"],
                    background_tasks=background_tasks,
                )
                payload = terminal_payload(stored)
                yield sse("status", {"label": "已命中完整回答缓存", "node": "turn_cache"})
                yield sse("delta", {"content": str(payload.get("answer") or "")})
                yield sse("done", payload)
                return
            async for item in run_agent_stream(x.message, "data-query-agent", context["history"], x.model_id, context["conversation_summary"], context["user_memories"], user["id"], x.session_id, turn["message"]["id"]):
                if item["event"] == "done":
                    response = response_from_state(item["state"])
                    store_cached_response(turn_cache_key, response, item["state"])
                    stored = finish_turn(
                        conversation_id=x.session_id,
                        user_id=user["id"],
                        client_message_id=client_message_id,
                        response_payload=response.model_dump(),
                        tool_result=item["state"].get("result") if item["state"].get("sql") else None,
                    )
                    await refresh_memory_after_success(
                        x.message,
                        x.session_id,
                        user["id"],
                        turn["message"]["id"],
                        item["state"].get("memory_actions"),
                        background_tasks=background_tasks,
                    )
                    payload = terminal_payload(stored)
                    yield sse("error" if stored.get("kind") == "error" else "done", payload)
                else:
                    yield sse(item["event"], {key: value for key, value in item.items() if key != "event"})
        except ModelProviderError as exc:
            log_event("api.stream_error", level=30, code=exc.code)
            stored = persist_chat_error(x.session_id, user["id"], client_message_id, str(exc), exc.code, request_id)
            yield sse("error" if stored.get("kind") == "error" else "done", terminal_payload(stored))
        except ModelOutputError as exc:
            log_event("api.stream_error", level=30, code="invalid_model_output")
            stored = persist_chat_error(x.session_id, user["id"], client_message_id, str(exc), "invalid_model_output", request_id)
            yield sse("error" if stored.get("kind") == "error" else "done", terminal_payload(stored))
        except Exception as exc:
            log_exception("api.stream_error", code="internal_error", error_type=type(exc).__name__)
            detail = "The service encountered an unexpected error. Please retry."
            stored = persist_chat_error(x.session_id, user["id"], client_message_id, detail, "internal_error", request_id)
            yield sse("error" if stored.get("kind") == "error" else "done", terminal_payload(stored))
        finally:
            finish_request(token)

    return StreamingResponse(events(), background=background_tasks, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
