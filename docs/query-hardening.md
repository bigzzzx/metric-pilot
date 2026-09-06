# 查询预检与模型调用可靠性（2026-09-06）

## 本次改动

- `backend/query_quality/sql_preflight.py`：当前 SQLite 只读工具在实际执行前先 `EXPLAIN` 编译，验证字段与歧义。预检和查询使用同一个连接、同一套目录字段 authorizer；原有表权限检查完整保留。预检不执行 SELECT，也不读取结果行。
- `backend/tools/builtins.py`：不再把字段、权限错误丢成笼统的 `tool_failed`。保留 `sql_schema_invalid`、`permission_denied` 和普通 `sql_error` 分类。
- `backend/agent.py`：缺字段或无权限立即结束查询，不进入 SQL 修复/结果分析；缺字段时提示确认字段和时间口径，不自动替换成下单时间。普通 SQL 语法错误仍允许原有的一次修复。
- `backend/model_reliability.py`：只重试模型请求，不重试 SQL、ETL、MCP 等工具。连接/读取等暂时性错误，以及 HTTP 408/429/502/503/504，最多再试一次；400/401/403、非 JSON 成功响应等不做网络重试。原有 Pydantic 格式修复是独立的一次修正。
- 单次模型请求（包含其网络重试）最多 60 秒。图入口建立共享的 90 秒墙钟预算，后续模型调用只能使用剩余预算。它不是强制取消高风险工具的“整个任务截止时间”。流式回答一旦有可见文字，不再重试重放；取消时关闭流资源。
- 日志增加节点成功/错误状态、模型尝试次数、HTTP 状态和异常类型。沿用 request_id，不记录提示词、密钥、完整响应体或业务结果。

这是现有 SQLite 数据源的本地预检，不向模型额外发送完整 schema。未来接入其他执行引擎，需要对应的 prepare/validate 适配；此预检不证明任意 SQL 在业务语义上正确。

## 验证

```powershell
.venv\Scripts\python.exe scripts/test_backend_isolated.py
npm test
.venv\Scripts\python.exe scripts/evaluate_queries.py --output .data/evals/2026-09-06-hardening-offline.json
```

隔离后端测试 220 项通过，前端现有测试 10 项通过。离线 SQL 20 项通过，3 项需要模型行为的题目跳过。隔离入口在 D 盘临时目录初始化平台库、演示库，使用 hash embedding/SQLite；不代表远端 Milvus、BGE、rerank 服务的验收。

真实模型使用原有配置和合成数据，报告保持原有 v2 题目与评分标准：

- `.data/evals/2026-09-06-hardening-live.json`：6 项通过 5 项。8 月 GMV=150、9 月追问=300、一对多去重=150、无权限提示、问候均通过。支付时间题在规划节点遇到读取超时，60 秒模型预算终止；没有 SQL 执行。
- `.data/evals/2026-09-06-hardening-payment-recheck.json`：单独补测支付时间题。模型仍编造 `p.pay_date`，但预检将其拦截，返回明确的字段/口径确认提示，没有运行 SELECT，也没有再调用模型修复。原评分要求规划阶段直接返回 `sql=null`，因此保留 **failed**，不把安全拦截冒充模型规划正确。

首轮普通问候首字约 1.42 秒；数据问题完整响应约 30～45 秒。本次重点是可靠性和可定位性，不能据单次样本声称性能提升。模型编造字段和供应商偶发超时仍然存在，防线已验证生效。

## Git 范围

本次新增模块、测试、隔离脚本，以及上次独立模块中的修改可单独提交。`agent.py`、`model_client.py`、`tools/sql_tool.py`、`tools/builtins.py` 与早前未提交工作重叠，继续保留在工作区，避免把不属于本次的整批改动混入提交。独立提交不是完整应用快照。
