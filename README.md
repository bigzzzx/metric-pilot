# 本地仿真 GenAI AWS Data Agent

无需 AWS 账号的面试演示项目：用自然语言查询本地电商数据、检索模拟 Glue Catalog，并触发模拟 Glue ETL Job。

## 启动

```powershell
npm run api
# 新开一个终端
npm run dev
```

打开 Vite 显示的地址（通常为 `http://localhost:5173`）。

## 启用 DeepSeek 对话 Agent

项目根目录的 `.env` 仅保存在本机，且已被 Git 忽略。首次配置时可复制 `.env.example` 并填入自己的 Key：

```dotenv
DEEPSEEK_API_KEY=你的密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
```

配置后重启 `npm run api`。模型会根据对话决定普通回答、目录问答、数据查询、受控 ETL 或安全拒绝；当问题要求原因、趋势、异常或建议时，会在查询后追加数据分析。没有 Key 或模型暂不可用时，自动降级为本地规则模式。

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
| LLM Agent | DeepSeek 结构化规划与结果分析；无 Key 时使用确定性规划器降级 |

`POST /api/chat`、`GET /api/catalog/search`、`GET /api/jobs/{run_id}` 与 `GET /api/openapi.json` 可用于 API 演示。

## 测试

```powershell
npm test
```
