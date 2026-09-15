# 指标 Wiki 与双路由实现说明

## 背景

原有数据查询统一走 Text-to-SQL。即使用户询问已经治理、口径固定的指标，也需要模型重新理解口径并生成 SQL，既慢，也存在同名指标选错、过滤条件遗漏和口径漂移的风险。

## 当前方案

数据类问题进入统一路由后，查询链路分为两条：

1. **Text-to-Metrics**：当前问题精确命中已发布指标别名时，绑定 `metric_id + version`，从受控指标合同编译 SQL。
2. **Text-to-SQL**：未命中治理指标时，继续使用 RAG、表结构检索和模型规划生成 SQL。

同名指标存在多个口径、用户无查询权限、指标未认证或合同无效时，系统直接返回澄清或权限说明，不会静默降级并猜测 SQL。

```text
data_query
  -> metric_router
       -> matched       -> metric_plan -> query -> analyze -> finish
       -> unmatched     -> rag -> data_query -> query -> analyze -> finish
       -> ambiguous/... -> finish
```

## 指标合同与 Wiki

每个可执行指标保存结构化合同，包括：数据源、资产、物理表、SQL 方言、聚合表达式、时间字段、固定过滤条件、可用维度及版本。模型不能修改合同中的指标口径，只能在复杂问题中提取日期、维度等查询参数。

指标 Wiki 是面向模型和用户的语义说明，包含摘要、详细口径、关联指标和证据引用。LLM 只生成待审核草稿；管理员发布时会校验字段、表达式、资产、业务域、关联指标和版本，只有审核发布的 Wiki 才进入 RAG。关联资产无权限时，相应 Wiki 也不可见。

这借鉴了 LLM Wiki/Graphify 的“实体关系 + 可检索子图”思路，但当前使用关系字段和 RAG 文档实现，没有额外引入图数据库。

### 三层 Wiki 的产品化映射

1. **Raw**：metric_wiki_sources 保存不可变原始内容、来源地址和 SHA-256。数据库触发器禁止更新与删除；修订必须新增来源。
2. **Wiki**：wiki_draft_json 保存模型草稿，wiki_json 保存管理员发布版本。每条声明都包含 source_ref 和原文 evidence。
3. **Schema**：backend/semantic_metrics/schema/ 保存显式规则，Pydantic 和发布服务执行同一套约束。

SQLite 是线上事实来源；Milvus 只是已审核 Wiki 的派生检索索引。发布事务会创建 metric_wiki_index_jobs，FastAPI 后台任务在事务提交后刷新 Milvus，并记录完成或失败状态。

## 安全与一致性

- 执行前再次检查指标版本、数据源状态、资产状态和用户权限，防止计划后配置被修改。
- 指标 SQL 只由合同编译器生成，并通过 SQLGlot 校验聚合表达式和字段。
- Text-to-Metrics 执行失败不会进入自由 SQL 修复，避免模型改变口径。
- 模型生成的 Text-to-SQL 即使引用了可执行治理指标，也会切换到合同编译器重新生成 SQL。
- 发布指标会改变目录指纹，使相关查询缓存自动失效。

## 性能验收

在关闭结果缓存、使用同一 DeepSeek 模型和同一组合成数据的条件下，各执行 3 轮：

| 路径 | P50 | P95 | 简单查询模型调用 |
| --- | ---: | ---: | ---: |
| 原 Text-to-SQL | 5075 ms | 6720 ms | 2 次 |
| Text-to-Metrics | 1716 ms | 1921 ms | 1 次 |

Text-to-Metrics 的 P50 总耗时降低约 **66.2%**。本地指标解析与 SQL 编译 100 次测试的 P50 为 **2.77 ms**、P95 为 **3.23 ms**。

真实 PostgreSQL 合成数仓只读验证中，合同编译 SQL 正确返回 `152355.00`，执行耗时约 **631 ms**。Wiki 草稿的模型生成约需 **18.7 秒**，但它只发生在维护/审核阶段，不进入每次查询链路。

## 验证命令

```powershell
python -m pytest
npm test
npm run build
python scripts/benchmark_semantic_metrics.py --live
python scripts/verify_metric_wiki.py
python scripts/verify_semantic_postgres.py
```

完整后端测试结果：`409 passed`；前端测试：`12 passed`；生产构建通过。
