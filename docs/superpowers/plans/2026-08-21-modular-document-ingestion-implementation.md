# Data Agent 多格式文档接入与长期记忆修复实施计划

1. 扩展长期记忆显式指令解析，排除疑问句，保存来源消息 ID，并对历史成功消息执行幂等回填。
2. 建立 `backend/knowledge` 领域包，定义统一文档、Block、Chunk、Locator 与解析异常契约。
3. 实现文件类型检测与 parser registry，接入 PDF、DOCX、XLSX、PPTX、HTML、Markdown、JSON/YAML、CSV、TXT 和 SQL 解析器；扫描 PDF 返回 `ocr_required`。
4. 实现结构优先、长文本语义细分、BGE token 上限及有限上下文继承的混合切片流水线。
5. 扩展 SQLite 文档/Chunk 元数据和原文件安全存储，保留现有 Milvus schema，通过 Chunk ID 从 SQLite 回填页码与章节信息。
6. 将知识库 API 移入独立 router，保留粘贴文本接口并增加 multipart 文件上传、文档详情与稳定错误码。
7. 将知识库前端拆成 API、页面和组件模块，支持二进制上传、格式提示、解析 warnings 与状态展示。
8. 补齐格式 fixture、切片不变量、上传鉴权、回滚、长期记忆跨会话/跨用户和历史回填测试。
9. 安装依赖，执行完整 Python 测试、Node 测试、前端构建及本地 API/页面冒烟测试。
