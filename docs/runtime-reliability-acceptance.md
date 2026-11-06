# 运行可靠性与索引恢复验收

日期：2026-11-06（北京时间）。基线 `01ea433`，分支 `codex/runtime-reliability`。方案见 `runtime-reliability-plan.md`。

## 实现

- `backend/data_services/storage.py`：幂等迁移不可变执行快照、哈希、执行 token、心跳、租约；未知和待恢复运行继续占用任务互斥槽。
- `production_sql.py`：SQLite `BEGIN IMMEDIATE` / PostgreSQL 事务 advisory lock。业务数据与每目标 `<target>_receipts` 凭证同事务提交，零行也有凭证；结构不符停止执行。
- `production_runner.py`：获得仓库锁后与提交前都校验旧执行者资格；元数据事务中再次检查身份/运行状态，完成回执必须实际落盘。提交确认丢失不等于回滚。
- 管理员 `POST /api/data-services/production/runs/{id}/recover`：可靠凭证匹配则仅恢复元数据；在同一互斥屏障下确认无凭证并隔离旧执行者，才结束未提交运行。仓库/锁/身份不明保持 `unknown`。恢复不重放业务写入，重试仍需新审批。
- `runtime_recovery_monitor.py`：应用启动后定期将过期运行标为 `recovery_required`，不连仓库、不自动解锁或重试；旧格式缺乏凭证时只能人工核对。
- `index_worker.py`：统一领取目录/Wiki/血缘持久任务，CAS 身份、过期回收、指数退避（5/10/20/40 秒等，单次上限 300 秒）、默认最多 5 次，失败后管理员重置。离线 SQLite 模式保留 outbox，不自动耗尽重试预算。
- 目录发布、元数据变更批准/回滚同事务入队；HTTP 后台执行只是立即尝试，重启后仍可领取。向量全量/增量写共用独立锁库，不在平台库事务中等待网络；取锁后读当前授权发布投影。
- 前端“数据生产”新增核对恢复入口与明确未知状态；“关联与血缘”展示全部类型索引积压、次数、下次时间、失败和逐项重试。

## 验证分层

本节的通过数只代表固定合成用例，不是生产问答准确率或可用性 SLA。

| 层次 | 结果与证明范围 |
| --- | --- |
| 后台完整回归 | 549/549，234.30 秒，命令退出码 0；比基线 511 项新增 38 项。3 个既有 pytest/Starlette 警告仍在 |
| 生产恢复专项 | 16 项，包含四个真实子进程 terminate 窗口：登记后、仓库提交前、提交后、元数据登记后；另覆盖零行、并发锁、未知、凭证/来源不符、旧执行者与确认竞争 |
| 索引专项 | 15 项；真实 kill 消费者、过期领取、未过期不抢占、旧确认 CAS、跨进程写锁释放、退避死信/重试、事务 outbox、离线开关 |
| 恢复 HTTP | 28/28；真实 Cookie/session、审批、SQLite 事务与凭证，重放防护及权限。HTTP 中断窗口为真实提交后构造平台确认丢失状态，不等同 kill 测试 |
| 生命周期 | 3/3；后台扫描与应用启动/退出接线，无远程请求 |
| SQLite 平台恢复 | 39/39；实际事务及提交凭证，包含已提交但元数据失败后的恢复和重复恢复 |
| 真实 PostgreSQL | 39/39；独立 Ubuntu PostgreSQL 16，实际同事务提交凭证、补登记和重复恢复不重写数据；故障为注入元数据登记异常，真实 kill 窗口另在 SQLite 验证 |
| 血缘 HTTP/RAG | 57/57；保持撤回、Schema 与授权过滤，不依赖旧向量正文授权 |
| 前端测试/构建 | 12/12，构建通过；既存 ECharts 分块 >500 kB 提示仍在 |
| Edge 真实 React/HTTP | 数据服务/生产 9/9，血缘 11/11；独立合成服务器，不使用用户的业务库 |
| 恢复与索引展示 | 5/5；真实 Edge/React，恢复/索引响应采用模拟数据，只证明展示和按钮接线，不能替代上述 HTTP 恢复验收 |
| Milvus 脚本离线 harness | 3/3；fake Milvus/hash embedding 验证脚本八个逻辑检查及只清理本轮集合，不是实库验收 |

## 真实 Milvus：尚未通过

已实际通过现有 SSH 配置检查服务器：Milvus、embedding、etcd、MinIO 当时停止约两周。第一次连接失败；经显式验收开关临时启动原已停止的四个依赖后，健康端点可访问，但创建独立验收集合时服务内部报错：

`InvalidateCollectionMetaCache failed / node not match [expectedNodeID=2] [actualNodeID=3]`

这是实际观察到的服务错误，不据此猜测或宣称已修复根因。未删除 etcd 键、未改网络配置、未重建已有集合或数据。验收脚本只清理本轮随机命名集合与 SSH 隧道；仅本轮启动的四个依赖在 finally 中停止，恢复原状态，另一个项目容器未改动。

剩余验收需先单独排查该服务的节点/会话状态，再重跑真实 Milvus/BGE 用例。任务持久化/重启/旧消费者/授权过滤已离线验证，但不能以此声称真实向量服务恢复闭环通过。

## 使用与重跑

1. 升级前停止旧版执行进程，备份平台库和原材料；不要混跑无锁旧版和新版。新后端启动会执行幂等迁移，不重写旧业务数据。
2. 心跳过期只代表需要核对。管理员进入“数据目录 → 数据生产 → 核对中断并恢复”。新鲜运行返回 409；未知状态继续阻塞。原 `/reconcile` 接口兼容转入同一安全核对流程。
3. 默认 `CATALOG_INDEX_WORKER_ENABLED=1`，仅 Milvus 模式自动启动索引消费者；`PRODUCTION_RECOVERY_MONITOR_ENABLED=1` 只启用过期扫描。测试脚本显式关闭自动线程并注入临时连接。
4. “关联与血缘 → 查看最近索引任务”可查看积压和失败；管理员可逐项重试，不能抢占有效执行租约。

```powershell
.\.venv\Scripts\python.exe scripts/test_backend_isolated.py
npm run verify:runtime
.\.venv\Scripts\python.exe scripts/verify_data_platform.py --runtime-recovery
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-data-platform-postgres.ps1 -RuntimeRecovery
npm test
npm run build
# 依赖已可用时，仅使用独立合成集合：
npm run verify:runtime:milvus
# 如确认允许临时启动原已停止的四个依赖并结束恢复状态：
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-runtime-milvus.ps1 -StartStoppedDependencies
```

浏览器依次使用各自全新 `scripts/data_platform_ui_server.py` 临时实例运行 `verify_data_platform_ui.cjs` / `verify_lineage_ui.cjs`。`verify_runtime_ui.cjs` 在生产浏览器验收后复用同一个合成管理员，模拟恢复与索引任务响应；截图/报告在 `.data/runtime-acceptance/`，不提交 Git。

## 保留边界

- 当前是单平台库/单逻辑目录的跨进程协调。同一 Milvus 集合不能被不同平台库或无锁旧版同时管理；不是分布式共识/跨库事务承诺。
- 未部署 Airflow、CDC、通用 ETL 引擎；恢复不会自动重放任意写入。
- 可信提交凭证依赖仓库持久性和账号权限；丢失/被篡改/无法访问时保持未知，不以超时、目标表有几行或行数为零代替证明。
- 本轮无外部模型调用。没有对真实业务表写入，PostgreSQL 验收仅在独立临时容器运行并清理。
- PostgreSQL 39 项验收中曾修正评测器：待补登记的已提交运行可以保留 `run_failed` SQL 候选，恢复后另建成功版本；原断言“所有 FAIL 来源候选数都必须为零”过严。实际要求是失败证据不能成为可发布的成功血缘，修正后实库重跑通过。
- 完整回归曾出现所有用例通过但 Windows 临时日志清理失败；测试退出前现先解绑并关闭应用日志处理器，防止晚到日志重新打开 append 文件。最终重新全量运行 549 项且清理/退出成功，不掩盖清理错误。
- 尚未统一部署 Ubuntu 应用，也未完成整个平台的备份恢复演练；这是后续阶段。
