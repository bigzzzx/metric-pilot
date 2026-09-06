const STATUS_LABELS = {
  idle: '',
  uploading: '正在上传文件…',
  parsing: '正在解析、切片并写入索引…',
  pending: '文档正在等待处理。',
  processing: '文档正在解析和建立索引。',
  ready: '文档已完成解析并写入知识库。',
  completed: '文档已完成解析并写入知识库。',
  ocr_required: '该 PDF 主要由扫描图片组成，需要配置 OCR 后再导入。',
  failed: '文档导入失败。',
  error: '文档导入失败。',
};

export default function ImportStatus({ result }) {
  if (!result || result.status === 'idle') return null;
  const status = String(result.status || 'ready').toLowerCase();
  const tone = status === 'failed' || status === 'error'
    ? 'error'
    : status === 'ocr_required' || result.warnings?.length
      ? 'warning'
      : status === 'ready' || status === 'completed'
        ? 'success'
        : 'progress';
  const details = [
    result.parser && `解析器：${result.parser}`,
    Number.isFinite(result.chunkCount) && result.chunkCount > 0 && `${result.chunkCount} 个 Chunk`,
  ].filter(Boolean).join(' · ');

  return <section className={`import-status ${tone}`} role={tone === 'error' ? 'alert' : 'status'} aria-live="polite">
    <b>{result.message || STATUS_LABELS[status] || `文档状态：${status}`}</b>
    {details && <span>{details}</span>}
    {result.errorCode && <code>错误码：{result.errorCode}</code>}
    {!!result.warnings?.length && <ul>{result.warnings.map((warning, index) => <li key={`${warning}-${index}`}>{warning}</li>)}</ul>}
  </section>;
}
