# 问数评测与统一图执行

两种聊天 API 共用 `backend/graph_runtime/workflow.py` 中的一张 LangGraph。
`state.py` 定义输入、输出和整体状态；`transport.py` 将图 custom/update 事件转换成现有 SSE 协议。
最终生成节点在流式请求中直接发送供应商 token；SQL 规划、元数据回答和审批提示仍需得到完整结构化结果，不把它们的等待时间伪装成首 token 时间。

## 运行评测

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_queries.py
.\.venv\Scripts\python.exe scripts/evaluate_queries.py --mode live --cases august_gmv,followup,greeting --output .data/evals/live.json
```

默认使用独立临时 SQLite 数据库，不修改 `.data/demo.db`、真实目录、用户记忆或远端 Milvus。
数据覆盖退款、取消、NULL、零金额、跨月、无订单用户及一笔订单多次支付。
离线模式检查 20 个已知 SQL 的结果与权限，另外 3 个自然语言行为用例标记 skipped；它不是 LLM 正确率。
live 模式调用项目配置的模型和同一编译图，工具连接到隔离数据，检索使用夹具目录的权限过滤 BM25。
这不是线上混合检索验收，也不能外推为大数据量性能。

报告分开记录 passed/failed/error/skipped、节点耗时、端到端首次回复与总耗时。
结果比较忽略行顺序但保留重复行、NULL，浮点数使用容差。列按输出顺序比较，支持不同别名。
自然语言解读仍需人工检查：SQL 算对不代表模型的解释没有多说条件。

评测版本 `2026-09-06.2-explicit-order-date` 明确月度统计按 orders.order_date，
并另设 payment_time 用例要求对不存在的实际支付日期进行澄清。
旧版没有明确日期口径，其失败记录仍保留，不能将两版的得分直接当作优化前后准确率提升。

## 口径和示例

查询计划新增 metric_refs、time_column、filters、join_relationships。
明确提到的已发布指标必须引用；引用绑定服务端提供的内容指纹，执行前重新校验权限和定义，变化后停止查询。
该检查验证引用与版本，不证明任意 SQL 和公式等价。仍需结果评测检查退款过滤、粒度、一对多关联等。

`metadata/query_examples.json` 是受版本管理的示例文件，预置 3 条通过独立夹具验证的 SQL。
validation 字段明确标注 offline-fixture-v1，而非声称业务管理员审核过。
每条示例关联完整表、关系或指标指纹；当前用户不能访问或定义已变化时自动不召回，最多返回 3 条。
不得自动把执行成功的查询登记为业务正确示例。新增示例应补充独立预期结果、依赖证据和验证记录。

## 本次修复

- 移除流式入口重复的 if/elif 编排。
- 修复 SQLRepairOutput 被当成字典读取的问题。
- 分析使用当前结果，避免重复注入前一查询结果。
- 对路由明确标记的上下文追问，RAG 和指标/示例检索补入最近数据话题与摘要，限制长度且不新增模型调用。
- 分析提示包含实际执行 SQL，要求不宣称查询中不存在的过滤条件。
- 缺少元数据时提示当前权限范围内不可用，不确认该表不存在或擅自替换业务域。
