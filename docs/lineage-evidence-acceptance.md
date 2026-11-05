# 多来源表关联与字段级血缘验收

日期：2026-11-05（北京时间）。本轮继承轻量数据中台的统一指标服务与审批生产闭环，在独立 `codex/lineage-evidence` 分支实现；不自动合并 main。方案见 `lineage-evidence-plan.md`，上一轮范围见 `lightweight-data-platform.md`。

## 已交付

1. 不可变来源保存正文/文件、SHA-256、来源版本、作用数据源与资产、位置、任务/运行/参数；同一来源版本不允许换内容，重复导入幂等。状态、候选和审核单独乐观版本化；既有人工作品不被模型重写。
2. SQLGlot 28.10.1 解析 JOIN、别名、CTE/子查询、INSERT SELECT、CTAS/视图、真实 Schema 星号展开与字段位置。区分表关联、持久转换、字段值来源、过滤/连接/分组间接影响。普通 SELECT 只留下使用证据与查询输出，不虚构物理数据产出。
3. 新版本开始记录实际成功的只读工具/指标 API 查询，不把缓存命中记录为又执行一次。可显式采集最多 100 条查询及 50 次已保存生产运行；没有可靠历史 SQL/外部审计日志时不会反推“执行事实”。
4. 内部生产提交和元数据恢复接入字段映射，带 run/job/version/SQL/参数。恢复成功新增来源版本，保留原失败/待核对证据；失败记录不能发布为成功产出。常量/参数保留在查询输出和运行参数，不伪造源字段。
5. 实际数据库发现快照、历史目录基线、演示预置、解析设计、内部成功运行和外部事件明确区分。外部事件需要 namespace/name → 实际资产 ID 绑定，是“外部上报”而非内部验证。
6. 文档保存原文件与解析块位置；LLM 只提候选。逐条验证精确原文子串、位置、版本和实际字段，类型冲突拒绝批准。无模型、格式错误、无证据或超过预算有明确状态。导入 SQL 不执行。
7. 技术校验、只读采样、业务批准和显式发布分开。采样最多各 1000 行，仅单字段对的技术探测，不证明全量基数、复合 JOIN 或业务语义。没有同名自动批准、采样自动批准或执行写入来试探关系。
8. 新前端入口“数据目录 → 关联与血缘”，含来源导入/原文与原文件、候选类型/状态/来源筛选、审核、发布、拒绝、撤回、来源失效和索引重试。字段上游/下游最多 3 跳，值来源与条件影响分别显示。
9. RAG/Agent 仅使用当前有效、Schema 匹配、已批准发布且全部引用资产有权限的投影。旧向量只用于找 ID，再回到当前授权投影取正文；撤回、停用、字段限制、权限/Schema/约束快照变化立即过滤，缓存身份同步变化。

## 验证分层

| 验证 | 实际结果 | 证明范围 |
| --- | --- | --- |
| 完整后台回归 | 511/511 | 原有 464 项，加本轮 47 项；隔离合成平台/数仓 |
| HTTP + RAG 闭环 | 57/57 | 实际 Cookie/session 认证、导入/审核/发布、字段影响、Agent 检索、缓存与撤回过滤 |
| SQLite 平台闭环 | 31/31 | 现有服务/审批/真实事务，额外核对字段映射及失败证据 |
| 真实 PostgreSQL 平台闭环 | 31/31 | Ubuntu PostgreSQL 16、独立读写账号、真实审批生产事务与字段证据 |
| 真实 Edge 浏览器 | 11/11 | React 表单点击、Schema 验证、审核/发布、字段/条件血缘、撤回/失效、文件上传与移动端布局 |
| 前端测试 / 构建 | 12/12 / 通过 | 构建仍有既存 ECharts 大分块提示 |
| 当前真实模型 | 已执行 | DeepSeek `deepseek-v4-flash`，只发送合成 Schema 和合成业务说明 |

最终一次模型提取产生 2 个候选，精确引用与真实字段绑定检查通过，均保持待审核，没有自动发布；耗时约 12.15 秒。早先一次返回 3 个候选并触发格式修正，说明生成有波动，不能把“引用合法”当作业务正确性，更不能以此报告生产召回率。

黄金 INSERT 地区汇总的值来源严格为：`order_time → stat_date`、`region → region`、`order_amount → paid_amount`；客户 ID JOIN 为使用候选，PAID 过滤和分组为独立间接影响。覆盖歧义/缺失字段、跨源同名、大小写与引号、未知动态语法、COUNT(*) 行集、类型冲突、Schema 变化、撤回、权限隔离、失败事务/恢复版本及错误引用等负例。通过率是固定合成用例通过率，不是生产发现完整率或通用 Agent 问答准确率。

PostgreSQL 使用本次独立创建的 `metric_pilot_acceptance` 临时实例，限制 256 MiB / 1 CPU，业务测试数据在 tmpfs；凭据不进入命令参数、报告或 Git。完成后仅清理该实例和 SSH 隧道，原有运行/停止容器未改动。

## 性能

最终离线报告每条路径 30 次，逐次检查正确结果；无 LLM、无应用结果缓存。解析使用已绑定的内存 Schema；字段接口是本地 TestClient 完整 ASGI 路径，不含浏览器 TCP 或真实用户等待审核。

| 路径 | P50 | P95 |
| --- | ---: | ---: |
| 黄金 SQL AST + 字段解析 | 4.61 ms | 5.24 ms |
| 字段血缘 HTTP / 授权投影 | 14.63 ms | 15.26 ms |

20 个黄金候选的导入及自动化逐个审核/发布全流程约 968.5 ms，不含真人审批等待。本轮不在每次聊天前调用模型提取血缘：SQL 捕获为小型本地记录，解析由显式采集或生产完成触发；文档模型提取由管理员单独发起。上述小规模数据不能外推到大型企业 Schema 或生产 SLA。

本次真实 PostgreSQL 指标 API P50/P95 约 830.17/915.56 ms，服务执行约 808.77/894.63 ms；30 次结果均验证黄金金额。该耗时含 SSH 网络、真实库访问和查询记录，不是 LLM 首 token 指标，不能与前轮环境当作严格等条件对比。

## 重跑与定位

```powershell
.\.venv\Scripts\python.exe scripts/test_backend_isolated.py
npm test
npm run build
npm run verify:platform
npm run verify:platform:postgres
.\.venv\Scripts\python.exe scripts/verify_lineage.py --output .data/evals/lineage-offline-latest.json
# 仅获准发送合成材料后：
.\.venv\Scripts\python.exe scripts/verify_lineage.py --live-model --output .data/evals/lineage-live-latest.json
```

浏览器：启动 `scripts/data_platform_ui_server.py`（3302）和 `npm run dev -- --config scripts/data-platform-vite.config.js`（5375），将 `NODE_PATH` 指向含 Playwright 的依赖目录，执行 `node scripts/verify_lineage_ui.cjs`。后端为一次性数据目录，不能拿这套脚本操作真实项目库。原始截图及报告在 `.data/lineage-acceptance/`，本地性能报告在 `.data/evals/`，均不提交 Git。

Windows 沙箱中的异步/本地 HTTP 测试出现过挂起，改为授权的本机进程运行相同隔离套件。另修正了测试 Agent 未绑定 SQL 工具、独立平台验收数量从 27 增至 31 的断言；浏览器发现并修正了审核输入因版本刷新丢失、表单无明确可访问名称、目录全局样式覆盖字段端点排版等问题。

## 边界

- 没有部署外部查询审计采集器、Airflow 集群、Spark/Flink 或 CDC。已有 Airflow 模板仍只是接入模板。
- OpenLineage 是当前常见 `columnLineage` 子集适配，不宣称完整协议/所有 transformation facet 兼容。
- 动态 SQL、UNION/递归 CTE、MERGE/UPDATE/DELETE 血缘等返回未解析/不支持，不由模型补事实；CTAS/视图目标须先有真实目录 Schema。
- 原始来源为审计保留，失效不是物理删除；大规模存储保留/对象存储、外部权限体系和生产规模压测仍是部署扩展项。
- 离线环境未运行 Milvus，因此索引任务保留 pending 并可重试；验证了结构化/词法检索与旧向量回查过滤，没有把它称为真实 Milvus 集群验收。
