# MCP 与 Skill 运行时设计

## 目标

把当前仅能录入配置的 MCP、Skill 页面升级为可发现、可绑定、可执行、可审计的运行时子系统，并复用项目现有的 Tool Registry、权限校验、Human-in-the-loop 和 LangGraph 路由能力。

## 已确认的产品边界

- MCP Server 自动发现的工具默认启用。
- 工具不会自动开放给全部 Agent；只有明确绑定对应 MCP Server 或工具的 Agent 才能使用。
- 不把所有新发现工具统一判为高风险。
- MCP annotations 用于辅助判断工具影响，但不能替代本地权限、审批与审计规则。
- 查询类工具可直接执行；数据写入、ETL 修改等高影响操作继续使用现有审批机制。
- Skill 是可复用的行为规范，不是可执行工具。
- Agent 未绑定 Skill 时不再加载全部 Skill。

## MCP 数据模型

### MCP Server

保留现有 Server 配置并补充：

- 名称、说明、Transport、Endpoint
- 认证方式与加密凭据引用
- 请求超时、启用状态
- 最近测试、同步时间和状态
- Server 能力与协议版本

### MCP Tool

新增持久化工具目录，核心字段包括：

- Server ID、工具名、显示名、说明
- 输入和输出 JSON Schema
- annotations、execution、原始元数据
- 启用状态、自动判断的风险等级、本地权限要求
- schema hash、最近发现时间、最后调用状态

工具运行时 ID 使用 `mcp:{server_id}:{tool_name}`，避免多个 Server 的同名工具冲突。

## MCP 生命周期

1. 管理员新增或编辑 MCP Server。
2. 后端通过官方 MCP SDK 建立 Streamable HTTP 会话。
3. 完成协议初始化并分页调用 `tools/list`。
4. 根据 Server ID 和工具名增量同步工具目录；保留管理员设置。
5. Agent 管理页面绑定 MCP Server 或具体工具。
6. LangGraph 根据当前 Agent 的绑定集合选择候选工具。
7. 工具参数经过 Schema 校验后进入统一 Tool Registry 执行器。
8. 执行器完成权限检查、审批、调用、超时、结果规范化和审计。

Server 支持工具列表变更通知时，后续可监听变更并自动刷新；首版保留手动刷新和定期刷新入口。

## 风险与权限

自动风险等级只用于 UI 和审批策略选择：

- 明确声明只读且不产生破坏性影响：低风险。
- 明确声明破坏性更新：高风险。
- 其他或声明不完整：中风险。

管理员可调整自动判级。是否允许调用最终由以下条件共同决定：

1. MCP Server 和工具均启用；
2. 工具已绑定当前 Agent；
3. 当前用户具备工具要求的权限；
4. 高影响操作已获得本次审批。

## MCP 与 LangGraph 集成

采用“Tool Registry 适配器 + 结构化工具选择节点”，而不是把 MCP 调用直接写进路由节点。

- MCP 适配器把远程工具转换成统一 ToolSpec。
- 路由模型只接收当前 Agent 可用工具的名称、说明和精简参数摘要。
- 结构化选择结果包含 tool ID、调用原因和参数。
- Tool Registry 统一执行本地工具和 MCP 工具。
- 工具结果重新进入 Agent 节点生成自然语言回答。

这种方式可继续兼容当前项目的多供应商 HTTP 模型调用，并保持权限、审批、审计只有一套实现。

## Skill 数据模型

Skill 补充以下字段：

- 名称、说明、完整指令
- 适用场景、标签和正反示例
- 优先级、是否始终启用、启用状态
- 当前版本、修改说明、创建人与更新时间
- Agent 绑定关系

版本发布后保留历史快照，Agent 默认使用当前已发布版本。

## Skill 选择与注入

1. LangGraph 路由阶段读取当前 Agent 已绑定 Skill 的名称、说明和适用场景，不加载完整内容。
2. 路由结构化输出本轮需要的 Skill ID。
3. 后端只加载选中 Skill 的完整指令；`always_on` Skill 始终加入。
4. Skill 内容使用明确的分隔块加入 Agent System Prompt。
5. 设置最大 Skill 数量和总字符预算，超限时按优先级裁剪。

Skill 不得绕过 Agent 工具绑定、用户权限或审批机制。

## 前端设计

### MCP 页面

- Server 搜索、状态筛选与新增入口
- Server 详情编辑、启停、测试连接、刷新工具
- 认证类型、Endpoint、超时和凭据更新
- 已发现工具列表、工具说明、Schema、annotations、风险和状态
- 单工具启停、权限配置和 Agent 绑定查看
- 最近同步、调用错误和审计入口

### Skills 页面

- Skill 搜索、状态筛选和列表
- 指令、适用场景、标签、示例、优先级和 always-on 编辑
- 启停、复制、归档和删除
- 版本历史和发布
- 已绑定 Agent 展示
- 最终 Prompt 注入预览

## 错误处理

- 连接、认证、协议、Schema、超时和远程工具错误分别返回稳定错误码。
- MCP 凭据不返回前端，日志中进行脱敏。
- `tools/call` 的文本和 structured content 统一转换为项目 ToolResult。
- Server 暂时不可用时不删除已发现目录，但标记不可用并向用户返回可定位的错误。

## 测试范围

- MCP Server CRUD、认证脱敏、连接测试和工具同步
- 分页 `tools/list`、工具更新与删除标记
- MCP 参数校验、调用结果和错误转换
- Agent 绑定、用户权限与审批路径
- Skill CRUD、版本、绑定、选择和上下文预算
- 未绑定 Skill 不注入、未绑定 MCP 工具不可调用
- 前端 MCP/Skill 管理交互和错误状态
- 使用本地测试 MCP Server 完成端到端 LangGraph 调用

