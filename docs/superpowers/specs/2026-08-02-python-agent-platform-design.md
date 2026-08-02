# Python Agent Platform 迁移与模型管理设计

## 目标

将 Node 原型迁移为 Python Agent 平台：FastAPI 提供 API，Pydantic 约束模型输出与配置，LangGraph 编排单一对话 Agent 的状态流。React 前端迁移到 TypeScript，并新增模型供应商与 Agent 管理工作区。

## 技术架构

```text
React + TypeScript / Vite
            │
FastAPI + Pydantic
            │
LangGraph: plan → guarded tool execution → optional analysis → response
            │
Provider Adapter (OpenAI-compatible HTTP)
            │
SQLite: demo data + local platform configuration
```

Python 服务分为：`api`（FastAPI 路由）、`agent`（LangGraph 状态机）、`llm`（供应商适配器）、`tools`（Catalog、SQL、ETL）、`security`（Fernet 凭据加密、SQL 审核）、`repositories`（SQLite 配置持久化）。Node 后端将被移除；前端保留 Vite，但改为 `.tsx` 和强类型 API 模型。

## 模型管理数据模型

- Provider：`id`、名称、类型（OpenAI Compatible / Ollama / vLLM）、Base URL、启用状态、加密后的 API Key。
- Model：关联 Provider，含模型 ID、显示名、能力标签（chat / tool calling / json）、启用状态。
- Agent：名称、描述、系统提示词、绑定的 Model、工具白名单。默认 Data Agent 绑定初始 DeepSeek 模型，允许使用 Catalog、Query 与 ETL 工具。

配置写入本机 SQLite 文件。API Key 使用 Fernet 加密保存，加密主密钥位于被 Git 忽略的 `.env`；管理接口只返回掩码 Key，永不回显明文。启动时从已有 DeepSeek 环境变量播种默认 Provider / Model / Agent。

## 统一 Agent 流程

LangGraph 的单一 Agent 每轮调用绑定模型，得到 Pydantic 结构化 `AgentPlan`：`CHAT`、`METADATA_QA`、`DATA_QUERY`、`ETL_TRIGGER`、`UNSAFE_REQUEST`。

- `CHAT` 与 `METADATA_QA` 直接给出自然语言回答。
- `DATA_QUERY` 经过只读 SQL 审核后查询 SQLite；模型要求分析或问题包含分析意图时，将结果限制为 50 行后进行第二次分析调用。
- `ETL_TRIGGER` 只允许受控 Job；危险操作不调用工具。
- 每个 `session_id` 保留有限历史和结果摘要，用于连续追问。

模型适配器使用 OpenAI-compatible Chat Completions：云供应商填写 API Key 与 Base URL；Ollama / vLLM 可填写本地 `/v1` 地址，Key 可为空或使用本地占位值。切换 Agent 绑定模型不修改 Agent 核心代码。

## API 与前端

- `/api/providers`：Provider CRUD、启停和连通性测试。
- `/api/models`：Model CRUD、按 Provider 查询和启停。
- `/api/agents`：Agent CRUD、选择模型与工具白名单。
- `/api/chat`：携带 `agent_id` 与 `session_id` 的对话。
- `/api/catalog/*`、`/api/jobs/*`：保留目录与任务状态。

前端新增“智能体管理”入口，包含“智能体”和“模型供应商”页：卡片列表、创建/编辑表单、模型绑定、连接测试、启用状态。聊天工作区提供当前 Agent 下拉选择；Data Agent 默认启用。

## 验证与边界

使用 pytest 覆盖 SQL 安全、凭据不可回显、Provider 配置、模型计划、LangGraph 路由及本地降级；前端执行 TypeScript 构建。真实 API 仅对默认 DeepSeek 做一条低成本连通性验证。

第一阶段不实现用户账号、多租户、云端密钥托管、真实 AWS、向量数据库或多 Agent 委派；但数据模型会允许后续扩展多个 Agent。
