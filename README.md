# 本地仿真 GenAI AWS Data Agent

无需 AWS 账号的面试演示项目：用自然语言查询本地电商数据、检索模拟 Glue Catalog，并触发模拟 Glue ETL Job。

## 启动

```powershell
npm run api
# 新开一个终端
npm run dev
```

打开 Vite 显示的地址（通常为 `http://localhost:5173`）。

## 可演示问题

- 查询上月华东地区各品类销售额
- orders 表中 amount 字段含义是什么？
- 刷新昨日订单宽表
- 查询销售额，修复错误字段
- 删除旧订单表

## 技术映射

| AWS 概念 | 本地演示实现 |
| --- | --- |
| Glue Data Catalog | `metadata/catalog.json` + 关键词 RAG |
| Athena | Node 内置 SQLite 查询工具 |
| Glue Job | 内存异步任务与状态日志 |
| LLM Agent | 确定性规划器；可替换为 Ollama 适配器 |

`POST /api/chat`、`GET /api/catalog/search`、`GET /api/jobs/{run_id}` 与 `GET /api/openapi.json` 可用于 API 演示。

## 测试

```powershell
npm test
```
