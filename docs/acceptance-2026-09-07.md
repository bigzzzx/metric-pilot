# 隔离端到端验收收尾（2026-09-07）

## 结论

本轮验收执行、已确认修复和回归已完成。模型供应商的单次空回答现在可有限恢复，
真实连续追问已通过；历史失败证据仍保留，不能据一次成功断言上游不再产生空回答。
本轮没有改写既有用户数据、日常模型配置或正式 Milvus collection，没有推送 GitHub。

## 已完成

| 项目 | 结果与边界 |
| --- | --- |
| 后端完整隔离回归 | 237 passed，38.00 秒；3 个依赖测试警告，无失败 |
| 前端测试和生产构建 | 10 passed；Vite build 成功 |
| 知识库离线验收 | MD/TXT/CSV 上传、真实解析切片、去重、检索、来源信息、跨用户隔离和重新打开持久库通过 |
| 远端真实 RAG | 同三类文件 3/3 通过，13.48 秒；真实 BGE 512 维、Milvus、BM25 混合召回与 rerank 分数均断言，不能靠静默降级通过 |
| HITL 接口验收 | 3/3：重建图和 checkpoint 连接后批准/拒绝、跨用户拒绝、重复与并发批准不重复写入；SQLite trigger 统计实际写入次数 |
| 真实浏览器 | 注册登录、管理员/分析师差异、32 次扩展标签切换无 pageerror、流式问候、刷新恢复历史通过 |
| 权限和数据目录 | 非管理员管理接口 403、其他用户对话 404、业务域过滤、不可见资产 404、示例上限和姓名脱敏、禁止普通用户新增数据源通过 |
| 真正重启后端 | 待审批对话两条消息恢复一致；浏览器批准实际更新 1 行；重复批准返回 409；刷新后不再出现批准按钮 |
| 连续上下文追问 | 8 月整月 PAID 合计 150、追问 9 月合计 300、解释 PAID、切回闲聊全部通过；无浏览器 pageerror |

RAG 测试用唯一 `acceptance_knowledge_<uuid>` 临时 collection，结束即删除；未修改原有 collection。
来源信息检查到检索结果和模型 prompt context，不代表前端已支持可点击引用。
本轮远端文件链路仅覆盖 MD/TXT/CSV；不能据此宣称 PDF/OCR/Office 全链路已验收。
浏览器截图已保存，但本轮没有完成截图逐像素人工复核。

## 产品修复

`backend/agent.py` 的写操作规划提示以前可能让模型将“等待用户批准”理解为
`ready=false`，用自然语言反问是否批准，导致真正的审批卡片不出现。

现在明确：`ready` 只表示操作参数齐全、可以创建审批申请，不表示已经获准执行。
权限检查、持久化中断、批准按钮、执行前复核仍由后端负责，未放宽安全门槛。
新增回归测试；真实浏览器提交申请、重启后批准、实际写入也已验证。

## 已处理：模型返回 HTTP 200，但正式回答为空

最新失败请求：`req_b30893192655`，发生在 router 的结构化模型调用。
隔离服务额外记录了不含正文/密钥的响应形状：

```text
http_status=200
finish_reason=stop
content_chars=0
reasoning_chars=257
refusal_present=false
tool_calls=0
completion_tokens=79
connection=custom_proxy
```

说明这次已收到供应商链路的 HTTP 成功响应，但没有可供 router 使用的正式内容。
异常发生在 Pydantic 校验之前，不是 SQL 执行失败，也不是前端白屏。
目前不能仅凭这些字段断言是 DeepSeek 服务、代理还是协议参数造成的；三次简单请求探针
都返回非空，也不能证明复杂路由调用始终正常。

模型适配层现在将该情况分类为 `model_empty_content`，正式日志只保存状态码、结束原因、
正文/推理长度、工具调用数和 token 数，不保存正文、推理内容或密钥。结构化模型请求在
原有调用/图总预算内只重试一次；连续两次为空仍返回明确错误。非 JSON、400/401/403 等
错误不会因此重试。不会把 `reasoning_content` 当成正式答案，也不会重放 SQL、ETL、MCP
或其他工具调用。针对性测试覆盖首次为空后成功、连续为空、非 JSON 不重试和真实 HTTP
适配器首次空后成功。

修复后真实上下文链路 111.754 秒通过；本次所有结构化调用首次即成功，**没有现场触发重试**，
所以恢复分支由可控 HTTP 适配器测试验证，不能冒充真实上游故障复现。历史失败报告保留。
早前另有两个验收脚本自身问题已修正：
重启时重复覆盖隔离 Agent 配置会撤回迁移绑定；“按日期统计”可能合理地被理解为按天分组，
而测试期待整月一行。现在只在首次启动复制配置，并明确整月合计、不按天分组。
这些不能计为产品修复或模型正确率提升。

## 复现入口与凭证

- `scripts/acceptance_server.py`：仅限本地隔离验收，3301；从日常库只读复制加密供应商/模型/Agent 配置，数据另存 `.data/acceptance-browser/`。使用合成账户，不用于生产。
- `scripts/acceptance-vite.config.js`：5374，代理至 3301。
- `scripts/acceptance_browser.cjs initial`：初次验收并留下待审批请求。
- 停止并重新启动隔离后端后运行 `scripts/acceptance_browser.cjs resume`：恢复和批准。
- `scripts/acceptance_browser.cjs context`：单独重测连续追问，不重复写操作。
- `scripts/acceptance_provider_probe.py`：三次有限只读模型探针，只记录响应元信息。

```powershell
.\.venv\Scripts\python.exe scripts/test_backend_isolated.py
npm.cmd test
npm.cmd run build
# 远端开关仅在 SSH 隧道将 Milvus/BGE/rerank 映射到 19530/8081/8082 后使用
$env:DATA_AGENT_ACCEPTANCE_REMOTE_RAG='1'
.\.venv\Scripts\python.exe scripts/test_backend_isolated.py -k upload_retrieve_isolate_and_reopen
Remove-Item Env:DATA_AGENT_ACCEPTANCE_REMOTE_RAG
```

浏览器脚本需已安装 Playwright 和 Edge，可通过 NODE_PATH 指向本机的 bundled runtime。
本地凭证（被 Git 忽略）在 `.data/acceptance-browser/`：
`backend-junit.xml`、`remote-rag-junit.xml`、`browser-initial.json`、`browser-resume.json`、
`browser-context.json` 及带时间戳的历史报告、`api.log`、截图。
`backend-empty-recovery-junit.xml` 对应空响应修复后的 237 项回归；远端 JUnit 对应真实 RAG。
浏览器 resume 报告对应本轮早先实际进程重启，最新 browser-context 报告对应修复后的连续追问。
收尾按 PID 和命令行核对后关闭本轮 3301/5374 专用服务及 SSH 隧道；保留隔离数据与日志。
没有停止其他项目进程或服务器容器。

## 建议下一步（尚未实施）

优先补前端答案引用溯源：让数据回答可展开查看命中的目录、知识文档、切片和版本，
同时只暴露用户有权限的来源。之后接入一个独立只读 PostgreSQL/MySQL 示例数据源，验证
数据源连接、目录采集、权限过滤、SQL 方言与查询执行的完整适配，暂不扩展新的 Agent 类型。
