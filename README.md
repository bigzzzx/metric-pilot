# Data Agent（本地开发版）

面向数据目录检索、自然语言取数和结果分析的 LangGraph Agent。当前以本地电商 SQLite 数仓作为演示数据源，同时为云上数据源、自建集群和外部知识文件保留了扩展接口。

## 启动

首次恢复代码时先创建 Python 3.12 虚拟环境并安装前端依赖：

```powershell
py -3.12 -m venv .venv
npm ci
```

Windows 上可用 `requirements.windows.lock.txt` 安装本次测试的精确版本；其他平台使用 `requirements.txt`。`.env`、`.data` 中的账号、凭据、会话和数据库不属于 Git 代码快照，恢复部署需单独准备，切勿用 Git 回滚替代数据备份。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm run api
# 新开一个终端
npm run dev
```

打开 `http://localhost:5174`；FastAPI 默认运行在 `http://127.0.0.1:3001`。

新环境使用 `.env` 时可用 `.venv\\Scripts\\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 3001 --env-file .env` 启动后端。只有全新演示环境才运行 `scripts/seed_demo_warehouse.py`：它会重建 `.data/demo.db` 并重写演示元数据，已有数据时不要执行。`APP_ENCRYPTION_KEY` 必须是有效 Fernet 密钥，已有部署不得随意更换。

独立回归测试：`.venv\\Scripts\\python.exe scripts/test_backend_isolated.py`（临时库、离线向量配置，不依赖个人 `.env`）。

## 启用 DeepSeek 对话 Agent

项目根目录的 `.env` 仅保存在本机，且已被 Git 忽略。首次配置时可复制 `.env.example` 并填入自己的 Key：

```dotenv
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

也可以在“智能体管理 → 模型供应商”中配置 OpenAI Compatible、Anthropic Messages、Ollama 或 vLLM。LangGraph 会根据对话决定普通回答、目录问答、数据查询、结果分析或受控 ETL；各节点使用智能体管理中绑定的模型与权限。

## 知识库向量检索（Milvus）

知识库的文档、权限和切块元数据保存于本地 SQLite；BGE 切块向量写入独立的 Milvus 数据库 `data_agent` 与集合 `knowledge_chunks_bge_small_zh_v1`。检索时会按当前用户过滤，只返回共享知识或该用户上传的知识。

上传支持 PDF、DOCX、XLSX、PPTX、HTML、Markdown、TXT、SQL、JSON、YAML 和 CSV。处理流程先按标题、段落、页码、工作表、表格和代码等原生结构解析，只对超长自然语言段落使用 BGE 语义断点，最后执行 450-token 硬限制。扫描版 PDF 会明确标记为 `ocr_required`，不会假装已解析。

单文件上限为 20 MiB；Office ZIP、YAML 展开量、解析后字符/内容块、向量化批次和原文件总量都有独立安全上限。默认每个用户可保存 2 GiB 原文件、每个知识库 1 GiB，可通过 `KNOWLEDGE_OWNER_QUOTA_BYTES` 与 `KNOWLEDGE_BASE_QUOTA_BYTES` 调整。失败任务会清理原文件，异常中断的 `processing` 文档会在后续协调时转为可诊断的失败状态并补偿清理。

本机开发通过 SSH 隧道访问服务器上仅绑定内网的 Milvus：

```powershell
.\scripts\start-milvus-tunnel.ps1
npm run api
```

`.env` 中应配置 `VECTOR_STORE_BACKEND=milvus`、`MILVUS_URI=http://127.0.0.1:19530`、`MILVUS_DATABASE=data_agent` 与 `MILVUS_COLLECTION=knowledge_chunks_bge_small_zh_v1`。运行 `scripts/start-milvus-tunnel.ps1` 会同时建立 Milvus、BGE embedding 和 reranker 的安全隧道。知识库检索采用 BGE 稠密召回 + 中文 BM25 + RRF 融合 + cross-encoder 重排；若重排服务暂时不可用，会自动退回 RRF，不影响基础查询。若需要纯本地单元测试，可临时设为 `VECTOR_STORE_BACKEND=sqlite`、`EMBEDDING_BACKEND=hash` 与 `RERANK_BACKEND=none`。

## 会话与记忆

- 所有会话与完整消息持久化在 `.data/platform.db`，刷新或重启不会丢失。
- 当前会话保留最近消息，较早消息达到阈值后由模型压缩为会话摘要。
- 长期记忆按 `namespace → key → value` 组织：命名空间由服务端按租户、用户和记忆类型生成，称呼、回答偏好和工作约定等稳定信息使用可更新的语义 Key。
- 路由模型会在现有一次结构化调用中声明受 Pydantic 校验的记忆操作；“你可以叫我 xuan”这类自然表达可以自动保存。明确的“记住……”“记得……”等命令仍是确定性兜底；“你记得……吗？”等疑问句不会误写。
- 记忆只会在本轮成功回答后写入。敏感信息、临时请求、一次性任务和模型推测不会保存；读取时固定加载核心偏好，并按当前问题补充相关记忆，随后以独立、受限的系统上下文注入。

## 可演示问题

- 查询上月华东地区各品类销售额
- orders 表中 amount 字段含义是什么？
- 刷新昨日订单宽表
- 查询销售额，修复错误字段
- 删除旧订单表

## 核心架构

| 能力 | 当前实现 |
| --- | --- |
| Agent 编排 | Python + FastAPI + LangGraph，路由到 General、Data Query、Data Analysis、ETL 节点 |
| 数据目录 | `metadata/catalog.json`、`relations.json` 与远端连接器扩展接口 |
| 只读查询 | Python SQLite 工具模拟 Athena，只允许目录白名单内的 SELECT/CTE |
| RAG 知识库 | 多格式结构解析 + SQLite 元数据 + Milvus BGE 向量 + 中文 BM25 + RRF + reranker |
| 上下文管理 | SQLite 会话、最近消息窗口、长会话摘要、显式跨会话长期记忆 |
| 模型接入 | 可配置供应商、模型与智能体绑定，响应由 Pydantic 校验 |

交互式 API 文档位于 `http://127.0.0.1:3001/docs`。

## 测试

```powershell
npm test
$env:EMBEDDING_BACKEND='hash'
$env:VECTOR_STORE_BACKEND='sqlite'
.\.venv\Scripts\python.exe -m pytest -q
npm run build
```
