# PostgreSQL 查询接入与验收

前端仍为 http://localhost:5174/。Windows 将原后端 3001 纳入保留范围，开发 API 改为 http://127.0.0.1:18001，Vite 代理保持一致。`npm run api` 和 `npm run dev` 分别启动后端和前端；如需更换端口，在 `.env` 设置 `DATA_AGENT_API_PORT` 并重启两者。

## 已实现

- 已授权 schema lookup 返回 source_id、namespace、dialect。规划和修复使用对应 SQL 方言；选择不明确时澄清。实际执行再次按发布状态、源启用状态及权限核验。
- SQL 工具统一分派至 SQLite/PostgreSQL 适配器。源与 Schema 共同识别表，拒绝跨数据源 Join。
- PostgreSQL 使用 SQLGlot AST、作用域和字段校验；敏感字段在选择、过滤、关联、CTE、子查询和星号展开中均受限制。拒绝写语句、锁语句及未批准函数；数据库事务为只读，search_path 固定 pg_catalog。
- 查询最多 100 行，语句超时 10 秒、锁等待 2 秒、连接超时 5 秒。错误返回稳定分类，不将连接凭据或底层异常原文回显。
- 样例最多 5 行，敏感字段在 PostgreSQL 端投影为遮蔽值后返回。金额 Decimal 返回字符串以保持精度，日期时间使用 ISO 字符串。
- 成功查询的数据源与 SQL 随会话历史保留，连续追问仍获得来源上下文；不会绕过新一轮权限检查。

## 验证

后端全量 280 项通过；适配器拆分后相关 40 项补测通过；前端 10 项测试及构建通过。

实库验收脚本 `scripts/verify_postgresql_queries.py --llm` 使用当前已配置的只读连接，在临时目录内建立隔离元数据与权限。未发布、删除或改写用户目录资产，也未写入远端业务数据。

实际结果：ods.orders 共 1200 行；关联 ods.customers 得到 4 个区域、合计 1200 行；订单金额汇总 564000.00；按月统计成功；明细截断至 100 行；客户样例 5 行且姓名、手机号、邮箱已遮蔽。

当前配置模型完成两轮规划和真实执行：先查询总行数，随后按客户区域分组统计；两轮均使用验证过的 PostgreSQL source_id。此项验证覆盖规划及执行节点，不等同于浏览器全流程自动化验收。

用户当前远端目录为 ods.orders 已发布、19 张待审核。可直接问「查询 PostgreSQL 测试数仓 ods.orders 有多少行」。需要关联客户表时，先核对并发布 ods.customers，尤其确认其敏感字段设置。

## 范围

首版仅支持单数据源内只读查询。跨源联邦查询、PostgreSQL 写入/ETL执行、任意扩展函数、自定义类型查询不在本次范围。元数据中的敏感级别需审核确认；源库自由文本注释不会自动等同于权限配置。

事务处理参考 [Psycopg 官方事务文档](https://www.psycopg.org/psycopg3/docs/basic/transactions.html)。
