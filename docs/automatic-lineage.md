# 自动采集与指标血缘

目标：从已审核的数据资产向上追溯来源、向下定位受影响的指标，同时区分视图定义、查询使用和生产运行三类证据。现有目录、权限和指标契约仍是真相源；不引入另一套元数据平台。

## 参考与取舍

- [DataHub PostgreSQL 连接器与查询采集源码](https://github.com/datahub-project/datahub/tree/master/metadata-ingestion/src/datahub/ingestion/source/sql/postgres)：借鉴“目录/视图定义与查询历史分别采集”，复用现有 PostgreSQL 只读连接器和 SQLGlot，不复制连接器或启用查询统计扩展。
- [DataHub 指标和语义模型](https://docs.datahub.com/docs/features/feature-guides/metrics-and-semantic-models-best-practices#lineage-tracing-a-metric)：借鉴独立指标节点、字段依赖和语义定义来源。项目已有 `MetricContract`，无需再安装 dbt 或从普通日志猜业务口径。
- [PostgreSQL `pg_get_viewdef`](https://www.postgresql.org/docs/16/functions-info.html)：直接复用系统函数读取普通视图和物化视图的定义；解析不执行该定义。外部任务以后可按 [OpenLineage 运行事件](https://openlineage.io/docs/spec/object-model/)接入，当前保留显式绑定导入，不部署 Airflow 或 DataHub。

以上是文档和源码调研；以下工作流是本项目实现，实际验证范围另列。

## 怎么用

1. 重启新版后端和前端。既有合法、启用且已认证的指标契约会迁移补齐当前血缘，不改原口径。发布新契约时同事务生成新版本依赖；历史边留存，查询只返回当前版本。
2. 数据目录 → 数据源 → 选择 PostgreSQL → 自动采集间隔。默认关闭，设置为每 5 分钟或更长后保存；首次扫描不必等一个完整周期。也可以继续手动「采集表结构」。无需给只读账号提升权限，不启用扩展。
3. 元数据同步与变更审核中可展开本次视图定义，核对后批准。新资产仍需补全业务说明和字段说明、审核发布；旧资产发生定义变化，也需批准变更。源字段结构变化、定义更新或视图失去可见性时，旧视图血缘不再返回。
4. 指标中心 → 查看指标血缘，区分计算列、固定过滤、时间字段、可选维度；`COUNT(*)` 是行集依赖，不冒充某个字段。数据目录 → 关联与血缘，选字段和「下游影响」，可以反查依赖该字段（含上游过滤条件）的认证指标。查询路径最多 3 跳。Wiki 的“关联指标”不是派生关系。
5. 关联与血缘 → 自动采集，可查看扫描、解析和等待审核状态。`LINEAGE_COLLECTOR_ENABLED=0` 暂停消费，保留待处理记录；远端定时扫描和本地解析不依赖 Milvus。

## 行为与边界

- 平台确实执行成功的只读 SQL 记录源和 Schema 指纹，再由有界后台队列解析。重试幂等；无法确定历史 Schema 的记录不回填为当前事实。缓存命中不记为新执行。观察到 JOIN 只生成待审核候选，普通 SELECT 不生成物理产出边。
- PG 扫描有数据源粒度的任务租约，未变快照不再重复写入。只有已审核定义和当前观察匹配、端点可见、字段绑定有效，才自动发布视图的表/字段血缘；不能完整解析时标缺口。没有为视图推断可供 Agent 执行的 JOIN。
- 视图作为数据服务输入时，需要完整只读校验和当前上游查询权限。普通视图在查询事务内锁定并核对实时定义；物化视图使用不读取行的规划取得查询锁。视图查询跳过工具/完整回答缓存，不在下次扫描之前用旧缓存绕过校验。未支持的函数、受限字段和不完整依赖保持不可执行。
- 不代表任意 SQL、存储过程、所有外部历史任务都能自动采集。没有读取 `pg_stat_statements` 或广域审计日志；没有新增指标到指标的计算 DSL。没有接入外部调度器时，现有 OpenLineage 导入不是实时事件采集。

## 验证

```powershell
.\.venv\Scripts\python.exe scripts/test_backend_isolated.py
npm test
npm run build
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/verify-data-platform-postgres.ps1 -AutomaticLineage -Port 25473
```

最后一项通过项目现有 SSH 配置启动唯一命名的临时 PostgreSQL 16，仅监听服务器回环端口。只写隔离合成库，退出后只移除本轮容器与隧道。浏览器验收使用独立前后端和合成账号，运行 `scripts/verify_lineage_ui.cjs`。没有调用外部模型，不将离线检索测试当作真实 Milvus 验收，也不替换 Ubuntu 的常驻应用。

本轮验收记录（2026-11-14，北京时间）：

| 验证 | 结果和边界 |
| --- | --- |
| 全量隔离后端 | 556/556，含 7 项新增自动血缘、重试和缓存专项；3 项既有依赖警告 |
| Ubuntu PostgreSQL 16 + 真实 HTTP 会话 | 22/22，实际发现普通/物化视图、审核、发布指标、上下游查询；改定义前后实查黄金值 100 和 400，权限拒绝与过时定义拦截通过 |
| Edge 真前端 + HTTP | 13/13，覆盖指标血缘、固定过滤、字段下游影响、原有证据审核和撤回；合成 SQLite 后端 |
| 原有隔离验收 | 血缘 HTTP/RAG 57/57，恢复/索引 HTTP 28/28；不是实 Milvus 测试 |
| 前端测试与生产构建 | 12/12，构建成功；既有 ECharts 分块大小提示仍在 |

实库验收的第一次扫描由真实后台线程触发，第二次扫描以测试时钟推进至调度时刻，不需要等待五分钟。所有结果均为固定合成用例，不是生产血缘覆盖率或性能承诺。
