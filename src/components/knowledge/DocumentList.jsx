function formatCreatedAt(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false });
}

function documentStatus(document) {
  const status = String(document.status || 'ready').toLowerCase();
  const labels = {
    ready: '已就绪', completed: '已就绪', pending: '等待处理', processing: '处理中',
    ocr_required: '需要 OCR', failed: '失败', error: '失败',
  };
  return { status, label: labels[status] || status };
}

export default function DocumentList({ documents = [], loading = false, error = '', onRetry }) {
  return <section className="document-list">
    <h3>已导入文档</h3>
    {loading && <p role="status">正在加载文档…</p>}
    {!loading && error && <div className="document-list-error" role="alert"><p>{error}</p>{onRetry && <button type="button" className="outline-button" onClick={onRetry}>重试</button>}</div>}
    {!loading && !error && documents.length > 0 && documents.map(document => {
      const { status, label } = documentStatus(document);
      const warnings = Array.isArray(document.warnings) ? document.warnings : [];
      return <article className="document-item" key={document.id || document.document_id || `${document.name}-${document.created_at}`}>
        <div className="document-item-heading"><b>{document.name || document.filename || '未命名文档'}</b><em className={`document-status ${status}`}>{label}</em></div>
        <span>{[
          document.source_type || document.mime_type,
          document.parser,
          Number.isFinite(Number(document.chunk_count)) && `${Number(document.chunk_count)} 个 Chunk`,
          formatCreatedAt(document.created_at),
        ].filter(Boolean).join(' · ')}</span>
        {document.error_message && <p className="document-error">{document.error_message}{document.error_code ? `（${document.error_code}）` : ''}</p>}
        {!!warnings.length && <ul>{warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}</ul>}
      </article>;
    })}
    {!loading && !error && documents.length === 0 && <p>暂无文档。可粘贴指标定义，或上传业务说明文件。</p>}
  </section>;
}
