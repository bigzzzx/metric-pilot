# 隔离端到端验收收尾（2026-09-07）

## 结论

本轮验收执行、已确认修复和回归已完成；**不代表所有真实模型链路通过**。
上下文连续追问仍会被供应商空回答阻断，保留失败证据，列为下一步优先事项。
本轮没有改写既有用户数据、日常模型配置或正式 Milvus collection，没有推送 GitHub。

## 已完成

| 项目 | 结果与边界 |
| --- | --- |
| 后端完整隔离回归 | 233 passed，40.78 秒；3 个依赖测试警告，无失败 |
| 前端测试和生产构建 | 10 passed；Vite build 成功 |
| 知识库离线验收 | MD/TXT/CSV 上传、真实解析切片、去重、检索、来源信息、跨用户隔离和重新打开持久库通过 |
| 远端真实 RAG | 同三类文件 3/3 通过，13.48 秒；真实 BGE 512 维、Milvus、BM25 混合召回与 rerank 分数均断言，不能靠静默降级通过 |
| HITL 接口验收 | 3/3：重建图和 checkpoint 连接后批准/拒绝、跨用户拒绝、重复与并发批准不重复写入；SQLite trigger 统计实际写入次数 |
| 真实浏览器 | 注册登录、管理员/分析师差异、32 次扩展标签切换无 pageerror、流式问候、刷新恢复历史通过 |
| 权限和数据目录 | 非管理员管理接口 403、其他用户对话 404、业务域过滤、不可见资产 404、示例上限和姓名脱敏、禁止普通用户新增数据源通过 |
| 真正重启后端 | 待审批对话两条消息恢复一致；浏览器批准实际更新 1 行；重复批准返回 409；刷新后不再出现批准按钮 |

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

## 未解决项：模型返回 HTTP 200，但正式回答为空

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
都返回非空，也不能证明复杂路由调用正常。没有把推理内容当成正式 JSON 或隐藏错误。

上下文追问测试仍为 failed；历史失败报告保留。早前另有两个验收脚本自身问题已修正：
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
后端 JUnit/远端 JUnit 对应本轮最新完整回归；浏览器 resume 报告对应本轮早先实际进程重启。
收尾已按 PID 和命令行核对后关闭本轮 3301/5374 专用服务及 SSH 隧道；保留隔离数据与日志。
没有停止其他项目进程或服务器容器。

## 建议下一步（尚未实施）

先做模型适配层稳定性：将空响应分类与脱敏诊断落到正式日志，核对供应商的结构化调用能力，
再设计在总预算内、尚无用户可见输出且未执行工具时的有限恢复，补异常响应测试。
不能重放 SQL/ETL/MCP 写操作，也不能将 reasoning_content 直接当答案。
之后重跑本轮失败的上下文链路。再考虑前端答案引用溯源和独立只读 PostgreSQL/MySQL 示例接入，
暂不扩展新的 Agent 功能。
