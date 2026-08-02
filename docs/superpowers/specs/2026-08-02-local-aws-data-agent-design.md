# 本地仿真 GenAI AWS Data Agent 设计

## 目标

构建一个无需 AWS 账号即可本地运行的面试演示项目。用户通过中文自然语言查询电商数据、了解数据目录，或触发一个参数化 ETL 任务。系统保留 AWS Glue Data Catalog、Athena 和 Glue Job 的接口语义，但默认使用本地实现。

## 范围

包含：

- React 单页聊天工作台和本地 Node HTTP API。
- 本地元数据目录与 RAG 检索。
- Agent 意图路由、工具选择、SQL 生成、只读安全审核和一次失败修复。
- SQLite 查询工具（模拟 Athena）和异步任务工具（模拟 Glue Job）。
- 可选 Ollama 集成；默认确定性规划器保证离线运行。
- 自动化测试和六个预置演示场景。

不包含登录、多用户权限、真实 AWS 调用、生产级消息队列或单独的向量数据库服务。

## 架构

```text
React / Vite Chat UI
     | HTTP
Node HTTP Agent Orchestrator
     |-- Catalog RAG Tool       -> metadata/catalog.json
     |-- Query Tool             -> SQLite + local ecommerce data
     |-- ETL Tool               -> simulated job runner + run history
     `-- Guardrails             -> read-only SQL and parameter validation
```

Agent 只能经由工具层访问目录、查询数据或启动任务。云服务通过适配器接口抽象：默认实现为本地适配器；以后配置 AWS 凭证时，可替换为 Glue、Athena 和 Glue Job 适配器，Agent 主流程保持不变。

## 组件边界

| 组件 | 职责 | 依赖 |
| --- | --- | --- |
| `agent` | 解析意图、编排检索和工具调用、输出可解释轨迹 | `rag`、`tools`、`guardrails` |
| `rag` | 按表名、字段、描述、业务词检索目录上下文 | 本地 catalog |
| `tools` | 提供目录检索、查询、任务启动与状态查询 | adapters |
| `guardrails` | 审核 SQL、校验 Job 与参数、拒绝危险请求 | SQL parser/规则 |
| `adapters` | 封装本地 SQLite、目录文件和模拟 Job | 文件系统、Node 内置 SQLite |
| `api` | 暴露 REST/SSE API 与请求模型 | agent |
| `web` | 对话、执行时间线、结果表格和 Job 状态 | API |

## 数据与目录

样例域为电商分析，包含：

- `users`：用户、注册日期、城市、地区。
- `products`：商品、品类、单价。
- `orders`：订单日期、用户、商品、数量、金额和状态。

`catalog.json` 为每张表记录描述、字段、字段业务含义、主键及关联键。例如 `orders.user_id -> users.user_id`、`orders.product_id -> products.product_id`。RAG 默认使用可解释的关键词/同义词打分，接口为可替换检索器；可选 Ollama 不改变目录检索结果格式。

预置一个 `refresh_order_wide` Job，只接收 ISO 日期 `run_date`，模拟“昨日订单宽表刷新”，写入结构化状态和日志。

## 请求流程

### 查询分支

1. 接收用户问题，确定性规划器或 Ollama 解析为 `ANALYTICS_QUERY`。
2. 检索相关表、字段和关联关系，形成受控上下文。
3. 生成结构化执行计划和一条 SQL。
4. Guardrails 仅允许单条 `SELECT` 或 `WITH ... SELECT`，并拒绝多语句、DDL、DML、未知表/字段及危险函数。
5. Query Tool 在 DuckDB 执行，返回列、行、行数、耗时与 SQL。
6. 如执行失败，使用脱敏错误和既有目录上下文进行一次修复；第二次失败则返回可行动的解释。

### ETL 分支

1. 识别为 `ETL_TRIGGER`，校验 job 名称和 `run_date`。
2. 启动模拟 Job，立即返回 `run_id`。
3. 前端轮询或订阅状态，展示 `QUEUED`、`RUNNING`、`SUCCEEDED`/`FAILED` 与日志摘要。

### 安全拒绝

对删除、更改或写入数据的请求，Agent 不调用任何执行工具，直接说明本演示的只读边界；拒绝事件仍出现在执行时间线中。

## API 与前端

- `POST /api/chat`：输入 `message` 和可选会话 ID；返回回答、意图、检索上下文、步骤事件和结果。
- `GET /api/catalog/search?q=`：返回匹配的目录条目，用于调试和演示。
- `GET /api/jobs/{run_id}`：返回模拟任务状态和日志。
- `GET /api/openapi.json` 提供轻量 OpenAPI 描述，供 API 演示。

前端是单页工作台，含输入框、聊天消息和可展开的执行时间线。查询消息显示命中目录、SQL、结果表、行数和耗时；ETL 消息显示进度与日志；安全拒绝显示具体原因。

## 离线模型策略

默认使用确定性规划器：基于中文意图词、目录检索结果和 SQL 模板生成工具计划。这确保零账号、零模型下载即可演示。

当 `LLM_PROVIDER=ollama` 时，Ollama 适配器使用相同的计划与工具结果 schema 生成决策和自然语言回答。模型输出永不绕过 SQL 审核或参数校验。

## 验收与测试

预置演示场景：

1. 上月华东各品类销售额聚合。
2. 用户与订单的跨表查询。
3. 询问“订单表中 amount 字段含义”的元数据问答。
4. 触发指定日期的订单宽表刷新。
5. 因错误字段失败后自动修复一次。
6. 请求删除旧订单表时被拒绝。

后端测试至少验证：目录检索、只读 SQL 白名单、未知对象拒绝、Job 参数校验、Job 状态转换、修复次数上限，以及上述六个端到端场景。前端至少验证聊天响应、执行轨迹和 Job 状态展示。

## 成功标准

在一台未配置 AWS 账户和 LLM 服务的电脑上，按 README 的步骤启动后，能在浏览器完成六个预置场景，并能在结果中解释目录命中、SQL 安全性、执行结果或 Job 运行状态。
