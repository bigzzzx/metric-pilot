# Data Agent Tool Registry 与高风险操作审批设计

日期：2026-08-30  
状态：待用户复核

## 1. 背景

当前项目已有工具名称清单、智能体工具绑定以及部分独立执行函数，但它们尚未形成统一的工具执行层：工具元数据定义在 `backend/extensions.py`，SQL 和记忆工具分散在 `backend/tools/`，RAG、分析和 ETL 则由 LangGraph 节点直接调用；权限、输入校验、异常处理和审计也分别位于不同位置。前端工具页只能展示静态目录，智能体管理页仍硬编码工具 ID。

项目后续会接入 SQL 写入、ETL 任务和外部 MCP 工具。这些操作可能修改业务数据或外部系统，不能仅凭模型决定立即执行。因此需要建立统一 Tool Registry，并将用户权限检查、风险策略和 Human-in-the-loop 审批放在所有工具都无法绕过的执行入口中。

## 2. 目标

- 建立统一的 Tool Registry，集中声明工具身份、输入输出 Schema、执行函数、权限和风险策略。
- 内置工具和 MCP 工具通过同一种执行结果协议进入 LangGraph。
- 工具执行前检查智能体绑定、工具启用状态、用户角色、业务域和数据资产权限。
- 对 SQL 写入、ETL 执行以及外部系统修改实施逐次审批。
- 使用 LangGraph 持久化 checkpoint 和 interrupt/resume，使待审批任务在刷新页面或服务重启后仍可恢复。
- 未授权请求不创建审批，直接明确告知缺失权限及申请方向。
- 审批只放行当前用户、当前工具和当前参数对应的一次执行，防止审批复用与参数篡改。
- 记录工具计划、权限判断、审批决定、执行结果、耗时和错误，形成完整审计链路。
- 工具目录和智能体工具选择从后端 Registry 动态读取，不再由前端硬编码。

## 3. 非目标

- 首版不提供“始终允许”或“完全访问权限”。高风险操作只能批准本次。
- 审批不能授予用户原本没有的权限，也不能绕过业务域、资产或字段级权限。
- 首版不实现通用工作流编排器，不让用户自由组合任意代码或 Shell 命令。
- 首版不实现具体生产 ETL 平台或所有数据库的写入协议；在真实连接器完成前，`sql_write` 和 `etl_execute` 保持关闭或返回稳定的 `tool_unavailable`。
- 不把通用对话和查询结果分析继续伪装成工具；它们属于智能体能力或 LangGraph 节点。

## 4. 方案选择

### 方案 A：只在现有节点前增加确认弹窗

改动较少，但权限和审批逻辑仍散落在各节点，新工具容易漏掉检查，刷新或重启后无法可靠恢复，不采用。

### 方案 B：Tool Registry + 自建待执行队列

Registry 负责统一执行，自建数据库状态机保存审批。实现可控，但会重复实现 LangGraph 已提供的中断、checkpoint 和恢复语义。

### 方案 C：Tool Registry + LangGraph interrupt/checkpoint（采用）

Registry 统一校验和执行；高风险工具通过 LangGraph `interrupt()` 产生审批请求，审批后以同一 `thread_id` 和 `Command(resume=...)` 恢复。SQLite checkpoint 保存图状态，业务数据库保存可查询、可审计的审批记录。该方案与当前框架一致，并能跨刷新和重启恢复。

## 5. 核心分层

```text
模型 / LangGraph 节点
        │ 只表达“希望调用哪个工具以及参数”
        ▼
Tool Registry
        │ 查找 ToolSpec、校验输入、解析执行器
        ▼
Tool Policy Engine
        │ 检查 Agent 绑定、工具状态、用户与资源权限、风险等级
        ├── 无权限 ──► PermissionDenied（不创建审批）
        ├── 低/中风险 ─► Tool Executor
        └── 高风险 ──► LangGraph interrupt
                              │
                        Approval Store
                              │
                    用户批准本次 / 拒绝
                              │
                    Command(resume=decision)
                              ▼
                        Tool Executor
                              │
                    输出校验与 Audit Log
```

各层职责：

- Registry 不决定何时调用工具，只负责工具定义和统一入口。
- Policy Engine 不执行工具，只给出 `allow`、`deny` 或 `approval_required`。
- Approval Store 不授予权限，只保存一次具体调用的审批状态。
- Executor 不重复做流程路由，只执行已通过所有前置检查的 handler。
- LangGraph 负责调用时机、暂停、恢复和后续回答。

## 6. 模块结构

```text
backend/
├── tools/
│   ├── models.py                 # ToolSpec、风险级别、统一结果模型
│   ├── registry.py               # 注册、查询、目录导出
│   ├── executor.py               # 输入/输出校验、handler 调用、超时和审计
│   ├── policy.py                 # Agent、RBAC、资源权限和审批策略
│   ├── approvals.py              # 审批仓储、参数摘要、过期与决定校验
│   ├── errors.py                 # 稳定错误类型和错误码
│   ├── builtins.py               # 内置工具集中注册
│   ├── catalog_tool.py           # 元数据/知识检索适配器
│   ├── sql_tool.py               # 只读 SQL；未来增加受控写入适配器
│   ├── memory_tool.py            # 记忆检索、写入和删除适配器
│   ├── etl_tool.py               # ETL 接口；无连接器时明确 unavailable
│   └── mcp_adapter.py            # MCP Tool Schema 转 ToolSpec 与调用适配
├── api/
│   ├── tools.py                  # 工具目录、详情和管理员启停
│   └── approvals.py              # 待审批列表、批准和拒绝接口
├── graph/
│   └── tool_execution.py         # LangGraph 工具执行/中断/恢复节点
└── checkpoint.py                 # SQLite LangGraph checkpointer 初始化

src/
├── api/
│   ├── tools.js
│   └── approvals.js
├── components/chat/
│   └── ToolApprovalCard.jsx
└── pages/extensions/
    └── ToolsPage.jsx
```

现有 `backend/agent.py` 首阶段只抽取与工具执行直接相关的节点，不进行无关的大规模重写。

## 7. ToolSpec 与数据契约

每个工具通过不可变 `ToolSpec` 注册：

```python
ToolSpec(
    id="readonly_sql",
    name="只读 SQL",
    description="在用户有查询权限的数据资产上执行一条 SELECT/WITH 查询。",
    input_schema=ReadonlySqlInput,
    output_schema=ReadonlySqlOutput,
    handler=run_readonly_sql,
    risk_level=RiskLevel.LOW,
    required_permission="data.query",
    allowed_agents=frozenset({"data-query-agent"}),
    timeout_seconds=30,
    enabled=True,
)
```

Registry 使用 Pydantic 校验工具边界，但不替换 LangGraph 的 `TypedDict` 总体状态：

- LangGraph State：保存整个流程状态。
- Tool Input/Output Pydantic Model：校验单次工具调用的参数和返回值。
- API Pydantic Model：校验审批接口和工具管理接口。

Registry 拒绝重复工具 ID。运行期只能通过 Registry 的 `execute()` 进入 handler，业务节点不得继续直接调用高风险 handler。

统一结果至少包含：

```json
{
  "status": "success | denied | approval_required | rejected | failed | unavailable",
  "tool_id": "sql_write",
  "call_id": "uuid",
  "data": null,
  "error": null,
  "audit_id": "uuid"
}
```

## 8. 工具分类与首版策略

| 项目 | 类型 | 风险 | 默认审批 | 说明 |
| --- | --- | --- | --- | --- |
| `catalog_rag` | 工具 | 低 | 否 | 仅检索当前用户可发现的目录和知识 |
| `readonly_sql` | 工具 | 低 | 否 | 继续限制为单条 SELECT/WITH，并执行资产和字段权限校验 |
| `memory_search` | 工具 | 低 | 否 | 仅检索当前登录用户的记忆 |
| `memory_write` | 工具动作 | 中 | 否 | 仅保存明确或稳定的非敏感信息 |
| `memory_forget` | 工具动作 | 中 | 否 | 用户明确要求删除时执行 |
| `sql_write` | 工具 | 高 | 每次 | 必须具有 `data.write` 且通过本次审批 |
| `etl_execute` | 工具 | 高 | 每次 | 必须具有 `etl.execute` 且通过本次审批 |
| MCP 只读工具 | 工具 | 按声明与管理员覆盖 | 通常否 | 默认最小权限 |
| MCP 修改型工具 | 工具 | 高 | 每次 | 外部系统写入、删除、发送、发布均要求审批 |
| `chat` | 智能体能力 | 不适用 | 不适用 | 从工具目录移除 |
| `query_result_analysis` | LangGraph 节点能力 | 不适用 | 不适用 | 从工具目录移除 |

MCP 工具不能自行声称“低风险”后绕过平台策略。未知 MCP 工具默认按高风险处理，只有平台管理员显式分类后才能降级。

## 9. 权限模型

权限检查顺序固定为：

1. 工具存在且启用。
2. 当前 Agent 已绑定该工具。
3. 当前用户已登录且账户有效。
4. 用户拥有工具所需平台权限。
5. 若工具指向业务域、数据资产或字段，继续检查资源级权限。
6. 通过权限后才判断是否需要审批。

首版新增权限动作：

- `tool.use`：使用一般工具。
- `data.query`：执行只读查询。
- `data.write`：执行 SQL 写入。
- `etl.execute`：启动或修改 ETL 任务。
- `tool.admin`：管理工具启停和风险策略。
- `approval.review`：审批需要独立审核人的操作，作为后续扩展预留。

平台管理员默认拥有全部动作；数据分析师默认只有目录发现、元数据查看、`data.query` 和低风险工具权限。权限可继续限定到业务域或具体数据资产。

权限不足时不调用模型猜测原因，也不创建审批请求。Policy Engine 返回结构化错误：

```json
{
  "code": "permission_denied",
  "tool_id": "sql_write",
  "required_permission": "data.write",
  "resource_type": "asset",
  "resource_id": "asset-orders",
  "message": "你没有向订单数据资产写入数据的权限，请联系管理员申请 data.write 权限。"
}
```

最终回答必须保留工具、缺失权限以及资源范围，不得只显示“执行失败”。对用户无权发现的资源只返回泛化范围，避免借错误信息泄露资产名称。

## 10. 审批模型

### 10.1 审批不是授权

只有已经通过 RBAC 和资源权限检查的用户才能进入审批。点击“批准本次”只是确认愿意执行这一项高风险操作，不会增加角色或权限。

### 10.2 审批绑定内容

每条审批记录绑定：

- `approval_id`
- `tool_call_id`
- `conversation_id` 与 LangGraph `thread_id`
- 发起用户和审批用户
- `tool_id`、工具版本和风险级别
- 规范化参数的 SHA-256 摘要
- 面向用户的脱敏参数摘要
- 资源范围、所需权限
- `pending / approved / rejected / expired / consumed / cancelled`
- 创建、过期、决定和消费时间

审批接口不接受新的工具参数。恢复时重新计算参数摘要，任何差异都使审批失效。一次审批成功消费后不可重复使用。

### 10.3 审批有效期

默认 15 分钟。过期后 Graph 保持可解释的终止结果；用户若仍要执行，必须重新发起并重新检查权限。权限在等待期间被撤销时，即使已批准，恢复执行前也必须再次校验并拒绝执行。

## 11. LangGraph 中断与恢复

高风险工具节点执行以下逻辑：

1. Registry 生成已校验的 `ToolCall`。
2. Policy Engine 返回 `approval_required`。
3. 保存审批业务记录，然后调用 LangGraph `interrupt()`，中断载荷只包含脱敏展示信息。
4. SQLite checkpointer 按 `thread_id` 保存图状态。
5. API 通过 SSE 发送 `approval_required`，当前 HTTP 流正常结束，不保持长连接等待人工操作。
6. 用户批准或拒绝后，审批 API 以 `Command(resume=decision)` 恢复相同 Graph。
7. 恢复节点验证审批状态、参数摘要、有效期和最新权限。
8. 批准则执行一次；拒绝或校验失败则返回结构化终止结果。

当前 `run_agent_stream()` 手工逐节点执行，无法直接获得 LangGraph 持久化中断语义。实施时将工具调用路径迁移到编译后的 Graph 流式运行；最终文本仍以 SSE `delta` 输出，审批中断以独立事件输出。

一个会话同一时间只允许存在一个活动中的高风险审批。用户取消会话、删除会话或发起互斥的新操作时，旧审批标记为 `cancelled`。

## 12. API 与 SSE 协议

### 工具接口

- `GET /api/tools`：按当前用户返回可见的 Registry 工具目录。
- `GET /api/tools/{tool_id}`：返回 Schema、风险、权限和状态；不返回 handler 或凭证。
- 管理员接口用于启停工具、覆盖 MCP 风险级别和查看审计记录。

### 审批接口

- `GET /api/tool-approvals?status=pending`：返回当前用户可处理的待审批项。
- `GET /api/tool-approvals/{approval_id}`：查看脱敏详情。
- `POST /api/tool-approvals/{approval_id}/decision`：请求体只接受 `approved` 或 `rejected`。
- 决定接口必须验证登录用户、CSRF 防护、审批状态和幂等键。

### SSE 事件

```text
event: approval_required
data: {
  "approval_id": "...",
  "tool_id": "sql_write",
  "tool_name": "SQL 写入",
  "risk_level": "high",
  "summary": "向 orders 更新符合条件的记录",
  "effects": ["可能修改 12 行数据"],
  "expires_at": "..."
}
```

审批决定后前端启动新的恢复流或按返回的恢复地址接收事件。恢复接口同样支持幂等，浏览器重试不能造成重复写入。

## 13. 前端交互

对话中展示审批卡片：

- 工具名称和高风险标签。
- 将要执行的操作摘要。
- 目标业务域或数据资产。
- 可能影响的记录数；无法预估时明确写“执行前无法准确估算”。
- 为什么需要该操作以及所需权限。
- “批准本次”和“拒绝”按钮。
- 审批中、已批准、已拒绝、已过期和执行结果状态。

按钮点击后立即禁用，防止重复提交。页面刷新后从待审批接口恢复卡片。前端不显示原始凭证，不直接拼接可执行 SQL；完整参数只保存在服务器端。

权限不足时展示独立权限提示卡，不显示批准按钮：

> 无法执行：你没有“数据写入”权限。  
> 所需权限：`data.write`  
> 资源范围：订单主题域  
> 请联系平台管理员申请对应权限。

工具扩展页改为动态显示工具状态、类型、风险、可用 Agent、输入字段摘要和最近测试状态。管理员可启停工具；非管理员只读。

## 14. 数据库与迁移

业务数据库新增：

- `tool_runtime_overrides`：启停、风险覆盖、超时和管理员配置。
- `tool_approval_requests`：持久化审批业务记录。
- `tool_audit_logs`：工具计划、权限、审批和执行审计。

LangGraph checkpoint 使用独立 SQLite 文件，避免与业务表迁移相互影响。数据库位于项目配置的数据目录，不写入 C 盘。启动迁移必须幂等，不删除现有 Agent、会话、记忆和目录权限数据。

审计日志不保存 API Key、密码和完整敏感字段值。工具参数通过每个 ToolSpec 的 `redact_input()` 生成脱敏副本。

## 15. 失败处理与安全

- 稳定区分 `tool_not_found`、`tool_disabled`、`agent_tool_denied`、`permission_denied`、`approval_required`、`approval_rejected`、`approval_expired`、`approval_tampered`、`tool_timeout`、`tool_failed` 和 `tool_unavailable`。
- handler 超时或异常不会自动重试写入型操作；只有明确具备幂等键的执行器才能安全重试。
- SQL 写入必须使用参数化语句或受控计划，禁止模型直接提交多语句脚本、DDL、ATTACH、PRAGMA 或任意数据库连接串。
- 可支持事务预览：审批前先展示目标表、操作类型和影响行数估计；实际执行时再次校验条件和权限。
- ETL handler 只能调用已登记的任务模板或外部编排平台 API，不能执行模型生成的 Shell。
- MCP 认证信息继续加密存储；MCP 返回值按不可信外部输入处理并进行大小限制。
- 服务重启后先恢复 `pending` 审批，不自动执行已批准但尚未消费的操作；必须由恢复流程再次完成校验。

## 16. 测试策略

### Registry 与契约

- 重复 ID、未知工具、禁用工具和不合法输入被稳定拒绝。
- handler 返回值不符合输出 Schema 时标记 `tool_failed`。
- 前端目录来自 Registry，新增工具不需要修改硬编码数组。

### 权限

- 未绑定工具的 Agent 不能执行。
- 分析师执行 `sql_write` 返回明确的 `data.write` 缺失信息，且数据库中不存在审批记录。
- 有平台权限但无目标业务域权限时仍拒绝。
- 无权发现的资产不会通过错误提示泄露名称。
- 等待审批期间撤销权限后，恢复执行仍被拒绝。

### 审批与恢复

- 高风险工具不会在批准前调用 handler。
- 批准本次后只执行一次；重复点击、网络重试和重复 resume 不会重复写入。
- 拒绝、过期、参数摘要变化和审批用户不匹配均不执行。
- 页面刷新和后端重启后待审批项仍可查询并恢复。
- 一个会话不能同时创建多个冲突审批。

### 安全与审计

- 审计日志包含调用人、工具、权限决定、审批决定、耗时和稳定错误码。
- API Key、密码和敏感字段不会出现在 SSE、审批卡片或日志中。
- SQL 写入和 ETL 超时不会被系统自动重试。

### 回归

- 只读 SQL、RAG、记忆检索、流式回答和历史会话继续通过现有测试。
- 低风险工具不因新增审批层产生额外模型调用。

## 17. 分阶段实施

### 第一阶段：统一工具执行层

建立 Registry、Pydantic 契约、Policy Engine、统一错误和审计；迁移 `catalog_rag`、`readonly_sql`、记忆工具；将 `chat` 和分析节点从工具目录移除。

### 第二阶段：持久化审批

引入 SQLite checkpointer、审批表、Graph interrupt/resume、审批 API 和 SSE 事件；使用不可写的测试 handler 验证中断恢复，不立即开放真实数据写入。

### 第三阶段：前端与管理

实现审批卡片、刷新恢复、权限不足提示、动态工具目录和管理员启停。

### 第四阶段：受控高风险连接器

在具体数据源支持事务、幂等和影响预估后启用 `sql_write`；在明确 ETL 平台和任务模板后启用 `etl_execute`。任何连接器都必须通过 Registry，不允许节点直接调用。

## 18. 验收标准

- 所有已启用内置工具均能从 Registry 查询，并通过统一入口执行。
- LangGraph 节点不再直接绕过 Registry 调用高风险 handler。
- 未授权用户得到包含缺失权限和申请方向的明确答复，且不会看到审批按钮。
- 已授权用户请求高风险操作时 Graph 在执行前暂停，页面显示审批卡片。
- 未批准、已拒绝、已过期或参数被修改的工具调用绝不执行。
- 批准本次后操作最多执行一次，并留下完整审计记录。
- 刷新浏览器或重启后端后，待审批任务仍可恢复或明确终止。
- 工具页与智能体管理页不再硬编码工具清单。
- 现有只读查询、RAG、长期记忆和流式对话功能不回退。
