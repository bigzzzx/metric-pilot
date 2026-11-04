# 轻量数据中台首轮验收

日期：2026-11-04（北京时间）。目标和实施范围见 `lightweight-data-platform.md`。

## 已交付

- 共享指标目录和结构化查询 API：绑定指标 ID/版本、治理后的日期/维度/筛选条件，与 Agent 共用契约编译入口；直接调用无需模型。
- 固定的已支付订单日汇总：SQLite 与 PostgreSQL 实际执行，逐次审批、同键幂等、分区重算、质量失败回滚。
- 可查询的运行记录、步骤、参数和 SQL；新产物沿用资产草稿/审核/发布流程。
- 成功运行登记实际源表→任务→汇总表血缘、质量统计与数据版本，令既有缓存身份失效。
- 数据提交后的元数据登记失败可由管理员恢复，不重复写入数据。
- 外部生产账号的请求可由平台管理员代审，原申请人的权限仍在执行前检查；个人 SQL/MCP 不开放跨用户审批。
- 前端“数据目录 → 数据服务 / 数据生产”，包括指标试查、登记任务、日期选择、批准/拒绝和步骤查看。

## 验收结果

| 验证 | 结果 | 范围 |
| --- | --- | --- |
| 后台完整回归 | 463/463 | 既有模块与新增指标/生产/权限用例 |
| SQLite 端到端 | 27/27 | 实际 Cookie 认证、ASGI HTTP 接口、审批、SQL、质量、血缘、缓存身份 |
| 真实 PostgreSQL 端到端 | 27/27 | Ubuntu 临时 PostgreSQL 16、独立读写账号、SSH 隧道、实际数据库事务 |
| 浏览器操作 | 9/9 | Edge 无头浏览器真实页面登记/试查/批准运行，含移动端布局与浏览器错误检查 |
| 前端已有测试/构建 | 通过 | `npm test` / `npm run build`；ECharts 仍有既存大分块提示 |

合成数仓的黄金答案：2026 年 8 月已支付订单金额 150；2026-08-01 的生产日汇总为金额 100、订单 1。再次按相同区间运行不会重复累计，输入金额改为 NULL 后运行明确失败，原汇总值仍为 100。

PostgreSQL 验收使用隔离的 `metric_pilot_acceptance` 数据库和内存数据目录，临时容器限制 256 MiB 内存和 1 CPU。密码只在进程内存和 SSH stdin 中传递。结束后验收容器及 SSH 隧道已清理，服务器原有运行/停止容器未改动。

## 性能

每种数据库 30 次结构化查询，全部校验黄金结果，不调用 LLM，也没有应用层结果缓存。数据库自身缓存可能已热。HTTP 指标使用本地 TestClient 的完整 ASGI 路径，不包含浏览器到 API 的真实 TCP 往返；PostgreSQL 数据库访问经过实际 SSH 网络。

| 路径 | P50 | P95 |
| --- | ---: | ---: |
| SQLite API（ASGI 全链路） | 45.70 ms | 57.00 ms |
| SQLite 服务执行 | 30.66 ms | 37.35 ms |
| PostgreSQL API（含数据库 SSH 网络） | 619.50 ms | 672.28 ms |
| PostgreSQL 服务执行 | 604.33 ms | 658.53 ms |

首次申请+自动化测试批准+实际生产执行的总耗时：SQLite 142.33 ms，PostgreSQL 625.31 ms。该测量不包含真实用户等待审批的时间；30 次合成样本不代表生产 SLA，也不能直接与含模型的自然语言问数耗时做等条件比较。

## 重跑命令

```powershell
.\.venv\Scripts\python.exe scripts/test_backend_isolated.py
npm test
npm run build
npm run verify:platform
npm run verify:platform:postgres
```

浏览器：启动 `scripts/data_platform_ui_server.py`（3302）和 `npm run dev -- --config scripts/data-platform-vite.config.js`（5375），将 `NODE_PATH` 指向已安装 Playwright 的目录，执行 `node scripts/verify_data_platform_ui.cjs`。只在该隔离环境使用合成测试账号。

## 尚未交付的独立部署项

- Airflow 提供了接入模板并核对官方接口，未安装/运行 Airflow 调度器；不宣称已完成无人值守调度。
- 用户自己的 PostgreSQL 写入 DSN、目标 Schema 权限、调度专用账号仍需按实际部署配置。本轮真实数据库验收已验证读写分离机制，没有扩大现有只读账号权限。
- 当前是固定业务模板的轻量生产链路，不是通用 ETL 编辑器、跨库 CDC 平台或具备分布式事务自动容灾的数据中台。
