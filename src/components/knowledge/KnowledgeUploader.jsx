import { useRef, useState } from 'react';
import {
  importKnowledgeText,
  normalizeImportResult,
  presentKnowledgeError,
  uploadKnowledgeFile,
} from '../../api/knowledge.js';
import ImportStatus from './ImportStatus.jsx';

const ACCEPTED_EXTENSIONS = ['pdf', 'docx', 'xlsx', 'pptx', 'html', 'htm', 'md', 'txt', 'sql', 'json', 'yaml', 'yml', 'csv'];
const FILE_ACCEPT = ACCEPTED_EXTENSIONS.map(extension => `.${extension}`).join(',');
const MAX_FILE_BYTES = 20 * 1024 * 1024;

export default function KnowledgeUploader({ knowledgeBaseId, onImported }) {
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState(null);
  const fileInput = useRef(null);

  const chooseFile = event => {
    const selected = event.target.files?.[0] || null;
    setStatus(null);
    if (!selected) { setFile(null); return; }
    const extension = selected.name.split('.').pop()?.toLowerCase() || '';
    if (!ACCEPTED_EXTENSIONS.includes(extension)) {
      setFile(null);
      event.target.value = '';
      setStatus({ status: 'error', message: `不支持 .${extension || '未知'} 文件，请选择支持的文件格式。`, errorCode: 'unsupported_format' });
      return;
    }
    if (selected.size > MAX_FILE_BYTES) {
      setFile(null);
      event.target.value = '';
      setStatus({ status: 'error', message: '文件超过 20 MB，请压缩或拆分后重试。', errorCode: 'file_too_large' });
      return;
    }
    setFile(selected);
  };

  const ingest = async event => {
    event.preventDefault();
    if (!knowledgeBaseId || busy) return;
    const form = event.currentTarget;
    const fields = new FormData(form);
    const content = String(fields.get('content') || '').trim();
    const name = String(fields.get('name') || '').trim() || '手动录入知识';
    if (!file && !content) {
      setStatus({ status: 'error', message: '请输入文本内容或选择一个文件。', errorCode: 'empty_document' });
      return;
    }

    setBusy(true);
    setStatus({ status: file ? 'uploading' : 'parsing' });
    try {
      const payload = file
        ? await uploadKnowledgeFile(knowledgeBaseId, file)
        : await importKnowledgeText(knowledgeBaseId, { name, content });
      const result = normalizeImportResult(payload);
      setStatus(result);
      if (!['failed', 'error', 'ocr_required'].includes(String(result.status).toLowerCase())) {
        form.reset();
        setFile(null);
        if (fileInput.current) fileInput.current.value = '';
      }
      await onImported?.(result, knowledgeBaseId);
    } catch (error) {
      const failed = {
        status: 'error',
        message: presentKnowledgeError(error),
        errorCode: error.code || 'import_failed',
        warnings: error.warnings || [],
      };
      setStatus(failed);
      // The indexer persists failed/ocr_required rows before returning some
      // non-2xx responses. Refresh even on failure so the document list is
      // always the source of truth for the latest durable status.
      await onImported?.(failed, knowledgeBaseId);
    } finally {
      setBusy(false);
    }
  };

  return <>
    <form className="knowledge-import" onSubmit={ingest}>
      <label>文档名称<input name="name" placeholder="例如：GMV 与复购率指标口径" disabled={busy || !!file} /></label>
      <label>粘贴文本<textarea name="content" rows="6" placeholder="例如：GMV = 已支付订单的 amount 之和；退款订单按业务规则排除。" disabled={busy || !!file} /></label>
      <div className="file-picker">
        <label>或选择文件
          <input ref={fileInput} type="file" accept={FILE_ACCEPT} disabled={busy} onChange={chooseFile} />
        </label>
        <span>{file ? `${file.name} · ${(file.size / 1024).toFixed(1)} KB` : 'PDF、Office、HTML、Markdown、文本及结构化数据，最大 20 MB'}</span>
      </div>
      <button disabled={busy}>{busy ? '正在处理…' : '解析、切片并写入知识库'}</button>
    </form>
    <ImportStatus result={status} />
  </>;
}
