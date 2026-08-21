# Data Agent 多格式文档解析与混合切片设计

日期：2026-08-21  
状态：待用户复核

## 1. 背景

当前知识库上传链路只支持 `txt`、`md`、`json`、`csv`。前端通过 `File.text()` 将文件当作文本读取，再以 JSON 请求发送给后端；后端的 `knowledge_base.py` 同时承担文本切片、向量化、数据库写入和检索职责。现有切片虽然会尽量在换行或句号处截断，但本质仍是 480 字符窗口与 80 字符机械重叠，不能可靠保留 PDF 页面、Markdown 标题、JSON 对象、Excel 表头、代码块等结构。

本设计将上传、解析、切片、向量化、持久化和检索拆成独立模块，并为常见业务文档提供结构感知切片。PDF 首版支持可搜索文本与表格；扫描件只检测并返回明确状态，OCR 通过稳定接口在后续接入。

## 2. 目标

- 支持 `pdf`、`docx`、`xlsx`、`pptx`、`html`、`txt`、`md`、`sql`、`json`、`yaml/yml`、`csv`。
- 采用真实二进制文件上传，不再由浏览器把所有文件强制转换成字符串。
- 优先按文档结构切片，仅对过长自然语言段落进行语义细分。
- 使用 BGE tokenizer 控制切片长度，避免文档被 embedding 服务静默截断。
- 继续使用现有 BGE 向量召回、中文 BM25、RRF 融合和 reranker 重排。
- 保留标题路径、页码、工作表、单元格范围、JSON 路径等来源信息，便于检索解释和问题排查。
- 将代码按职责拆分；FastAPI 入口只注册路由和依赖，不包含文档解析、切片或存储实现。

## 3. 非目标

- 首版不执行扫描 PDF 的 OCR，但提供 `OcrProvider` 接口和 `ocr_required` 状态。
- 不支持旧二进制 Office 格式 `.doc`、`.xls`、`.ppt`；接口返回转换为现代格式的明确提示。
- 不在首版解析图片中的图表含义，也不执行公式计算。
- 不用 LLM 逐段切片。语义边界使用已部署的 BGE embedding，降低成本和不确定性。
- 不替换现有 Milvus、BM25、RRF 或 reranker 方案。

## 4. 方案选择

### 方案 A：统一使用大型通用解析框架

使用 Unstructured 或 Docling 一类框架处理所有文件。开发快，但依赖体积大、不同格式的行为不透明，Windows 本地安装和部署成本高，难以针对业务表格与 SQL 调整。

### 方案 B：格式解析器适配器 + 共享流水线（采用）

每种文件族有独立解析器，统一输出内部 `ParsedDocument`，之后复用结构切片、语义细分、向量化和持久化流程。依赖可控、便于测试，任何一种格式出错都能单独定位和替换。

### 方案 C：外部文档解析服务

将复杂文档发送给独立服务。适合未来的大规模或 OCR 场景，但当前增加了部署、网络和权限管理成本，不适合作为本地演示首版。

## 5. 模块结构

```text
backend/
├── api/
│   └── knowledge.py                # FastAPI 知识库路由
├── knowledge/
│   ├── models.py                   # ParsedDocument、DocumentBlock、ChunkDraft
│   ├── service.py                  # 导入用例编排
│   ├── repository.py               # SQLite 文档与 Chunk 持久化
│   ├── source_storage.py           # 原始文件安全保存与读取
│   ├── vector_index.py             # Milvus 写入适配层
│   ├── retrieval.py                # 混合检索入口
│   ├── parsers/
│   │   ├── base.py                 # DocumentParser 协议与通用异常
│   │   ├── registry.py             # MIME、扩展名和文件签名路由
│   │   ├── text.py                 # txt、sql
│   │   ├── markdown.py             # md
│   │   ├── html.py                 # html
│   │   ├── structured.py           # json、yaml
│   │   ├── tabular.py              # csv、xlsx
│   │   ├── pdf.py                  # 可搜索 PDF 正文与表格
│   │   ├── word.py                 # docx
│   │   └── presentation.py         # pptx
│   ├── chunking/
│   │   ├── pipeline.py             # 切片策略调度
│   │   ├── structural.py           # 标题、段落、对象、表格、代码块
│   │   ├── semantic.py             # 长文本相邻句语义断点
│   │   ├── token_budget.py         # BGE token 上限与小块合并
│   │   └── context.py              # 标题继承和有限语义重叠
│   └── ocr/
│       ├── base.py                 # OcrProvider 协议
│       └── disabled.py             # 首版明确返回 ocr_required
└── main.py                         # 创建应用、挂载路由
```

每个模块只有一个主要职责。解析器不得直接访问 SQLite 或 Milvus；切片器不得读取 HTTP 请求；路由不得包含文件格式判断和切片算法。

前端同步拆分，避免继续扩大 `src/main.jsx`：

```text
src/
├── api/knowledge.js
├── pages/extensions/KnowledgeBasePage.jsx
└── components/knowledge/
    ├── KnowledgeUploader.jsx
    ├── DocumentList.jsx
    └── ImportStatus.jsx
```

## 6. 内部数据契约

解析器统一返回：

```python
ParsedDocument(
    title: str,
    source_type: str,
    blocks: list[DocumentBlock],
    metadata: dict[str, object],
    warnings: list[str],
)

DocumentBlock(
    block_type: Literal["heading", "paragraph", "list", "table", "code", "record"],
    text: str,
    section_path: tuple[str, ...],
    locator: dict[str, object],
)

ChunkDraft(
    content: str,
    embedding_content: str,
    token_count: int,
    section_path: tuple[str, ...],
    locator: dict[str, object],
    block_types: tuple[str, ...],
)
```

API 请求和数据库边界继续使用 Pydantic 校验；解析与切片内部模型使用小型、不可变的 dataclass，避免把 API、框架状态和领域对象混在一起。

## 7. 文件解析策略

| 文件类型 | 解析单元 | 保留的定位信息 |
| --- | --- | --- |
| PDF | 页面中的标题、正文块、表格 | 页码、表格序号、边界框（可用时） |
| DOCX | 标题、段落、列表、表格 | 标题路径、段落/表格序号 |
| XLSX | 工作表、表头、连续数据行 | 工作表名、行范围、列范围 |
| PPTX | 每页标题、正文、备注、表格 | 页码、元素类型 |
| Markdown | 标题、段落、列表、表格、代码围栏 | 标题路径、代码语言 |
| HTML | 标题、正文、列表、表格、代码块 | DOM 标题路径 |
| JSON/YAML | 对象字段、数组元素 | JSONPath 风格路径 |
| CSV | 表头与连续数据行 | 行范围、列名 |
| TXT | 段落、句子 | 字符范围 |
| SQL | 注释、语句、CTE 或逻辑代码块 | 语句序号、代码类型 |

解析依赖采用专用轻量库：pdfplumber、python-docx、openpyxl、python-pptx、Beautiful Soup、markdown-it-py、PyYAML 和 sqlparse。解析器通过 registry 注册，未来增加格式时不修改导入服务主体。选择 pdfplumber 而不是带 AGPL/商业双重许可约束的 PDF 依赖，使本项目后续商业化部署的许可边界更清晰。

## 8. PDF 处理

1. 校验 PDF 文件签名、大小、加密状态和页数。
2. 按页提取有阅读顺序的文本块，并尝试识别表格。
3. 表格转换为带表头的 Markdown 表格；跨 Chunk 时重复表头。
4. 每个 Chunk 保留页码和章节标题，回答时可以显示来源页。
5. 若多页几乎没有可提取文字但包含图片，文档状态设为 `ocr_required`，不把空内容写入向量库。
6. `OcrProvider` 接口接收页面图像并返回带页码的 `DocumentBlock`；以后可接 PaddleOCR、云 OCR 或独立 OCR 服务，而不改变上传、切片和检索接口。

## 9. 混合切片策略

### 9.1 结构优先

- 元数据表、字段、Join 关系和单条指标定义继续作为原子文档，不走通用长文本切片。
- Markdown、Word、HTML 和 PDF 先按标题路径形成 section。
- JSON/YAML 按对象路径切分；小对象保持完整，超大数组按元素批次切分。
- CSV/XLSX 表头与数据行绑定；一行不跨 Chunk。
- SQL 代码块和 Markdown 代码围栏保持完整；仅在超过硬上限时按语句安全边界拆分。

### 9.2 语义细分

只有自然语言 section 超过 token 目标时才执行：

1. 按中英文标点拆成句子。
2. 批量调用现有 BGE-small-zh-v1.5 生成句子窗口向量。
3. 计算相邻窗口余弦距离，在明显的主题跳变点断开。
4. 合并过小相邻块，并由 token guard 强制执行硬上限。

这避免对表格、代码和 JSON 使用不稳定的“纯语义”切分，同时把 embedding 调用限制在真正需要的长段落。

### 9.3 Token 与上下文

- 目标大小：约 320 BGE tokens。
- 硬上限：450 BGE tokens，为模型特殊标记留出余量。
- 最小建议大小：80 tokens；更小内容优先与同 section 相邻块合并。
- 不再固定复制 80 字符。自然语言块最多继承上一句；所有块继承简短标题路径。
- `embedding_content` 包含文档名与标题路径，`content` 保留用户可读正文，避免检索提示污染展示内容。

## 10. 上传 API 与前端

- 保留现有 JSON 文本导入接口，用于粘贴指标说明。
- 新增 `multipart/form-data` 文件上传接口，前端直接发送二进制文件。
- 新增文档详情/状态接口，返回解析 warnings、稳定错误码和可重建状态。
- 默认单文件上限 20 MB，最大 300 页；限制通过环境变量配置。
- 后端同时检查扩展名、MIME 和文件签名，不能只相信浏览器提供的类型。
- 导入响应返回 `document_id`、状态、解析器、Chunk 数、warnings 和明确错误码。
- 前端展示上传、解析、切片、向量化、完成/失败状态；失败时保留可读原因。

## 11. 持久化与索引一致性

- SQLite 继续作为文档与 Chunk 的事实来源，增加文档状态、MIME、文件哈希、解析器、warnings 和错误字段。
- Chunk 增加 `token_count`、`section_path`、`locator_json`、`block_types`。
- 原始文件使用系统生成的文档 ID 保存到项目 D 盘 `.data/uploads/<owner>/<document_id>/`；用户文件名只作为元数据，不能参与真实路径拼接。原文件用于重新切片与索引重建。
- Milvus 继续保存检索所需的 Chunk ID、文本和向量；丰富定位信息通过 Chunk ID 从 SQLite 回填，避免为新增元数据重建现有 Milvus schema。
- 文件哈希用于阻止同一知识库重复导入相同文件。
- 只有全部向量写入成功后文档才进入 `ready`；失败时不返回部分可检索结果。
- 旧文本 Chunk 保持可检索，新文档走新流水线；不强制一次性重建用户已有知识库。

## 12. 错误、安全与可观测性

- 使用稳定错误码区分：不支持格式、文件过大、加密 PDF、需要 OCR、无可读内容、解析失败、向量服务不可用和索引失败。
- 禁止执行上传文件中的宏、脚本或公式；Office 文件仅作为数据读取。
- 临时文件使用受控临时目录并在请求结束后清理；日志不记录原始文档正文。
- 记录 `document_id`、parser、页数/工作表数、block 数、Chunk 数、耗时和 request_id，便于定位问题。
- 日志模块与解析器解耦，解析器只返回 warnings 或抛出领域异常。

## 13. 测试策略

- 每种格式使用最小 fixture 验证标题、表格、定位信息和中文内容。
- 验证 Markdown 代码围栏、JSON 对象、Excel 单行和 PDF 表格不会被错误截断。
- 使用假的 embedding 客户端测试语义断点、过小块合并和 450-token 硬上限。
- 验证扫描 PDF 返回 `ocr_required`，加密 PDF 与损坏文件返回稳定错误码。
- 验证同文件重复上传、Milvus 写入失败回滚和用户知识库隔离。
- 对 GMV、字段别名、Join 关系和长业务文档建立检索回归用例，比较改造前后的 Recall@K 与 rerank 结果。
- 前端测试二进制 multipart 上传、进度状态、失败展示和刷新后的文档列表。

## 14. 验收标准

- 所有列出的现代文件格式都可通过同一上传入口导入。
- 可搜索 PDF 的标题、正文和表格能被查询，并能返回页码来源。
- 扫描 PDF 不会假装导入成功，而会明确提示需要 OCR。
- Chunk 不超过 BGE token 硬上限，且标题、表头、JSON 路径、代码块等结构得到保留。
- `main.py` 中不再包含知识库文档解析、切片和持久化实现。
- 单独替换某个解析器或语义切片算法不需要修改其他文件格式解析器。
- 现有元数据 RAG、混合检索、用户权限和对话功能测试继续通过。
