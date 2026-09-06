import { useEffect, useRef, useState } from 'react';
import {
  createKnowledgeBase,
  listKnowledgeBases,
  listKnowledgeDocuments,
  presentKnowledgeError,
} from '../../api/knowledge.js';
import DocumentList from '../../components/knowledge/DocumentList.jsx';
import KnowledgeUploader from '../../components/knowledge/KnowledgeUploader.jsx';
import { shouldRefreshKnowledgeDocuments } from './knowledgeSelection.js';

function asList(payload) {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload?.items)) return payload.items;
  if (Array.isArray(payload?.knowledge_bases)) return payload.knowledge_bases;
  if (Array.isArray(payload?.documents)) return payload.documents;
  return [];
}

function canWrite(base) {
  if (!base) return false;
  if (typeof base.can_write === 'boolean') return base.can_write;
  // The API only lists shared bases and bases owned by the current user, so a
  // non-null owner is a safe compatibility fallback for older payloads.
  if (Object.prototype.hasOwnProperty.call(base, 'owner_id')) return Boolean(base.owner_id);
  return true;
}

export default function KnowledgeBasePage() {
  const [bases, setBases] = useState([]);
  const [selectedId, setSelectedId] = useState('');
  const [documents, setDocuments] = useState([]);
  const [showCreate, setShowCreate] = useState(false);
  const [notice, setNotice] = useState('');
  const [pageError, setPageError] = useState('');
  const [documentError, setDocumentError] = useState('');
  const [creating, setCreating] = useState(false);
  const [loadingBases, setLoadingBases] = useState(true);
  const [loadingDocuments, setLoadingDocuments] = useState(false);
  const baseRequest = useRef({ sequence: 0, controller: null });
  const documentRequest = useRef({ sequence: 0, controller: null });
  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;

  const loadBases = async () => {
    const sequence = baseRequest.current.sequence + 1;
    baseRequest.current.controller?.abort();
    const controller = new AbortController();
    baseRequest.current = { sequence, controller };
    setLoadingBases(true);
    try {
      const data = asList(await listKnowledgeBases({ signal: controller.signal }));
      if (baseRequest.current.sequence !== sequence) return;
      setBases(data);
      setSelectedId(current => {
        const next = data.some(base => base.id === current) ? current : data[0]?.id || '';
        selectedIdRef.current = next;
        return next;
      });
      setPageError('');
    } catch (error) {
      if (error?.name === 'AbortError' || baseRequest.current.sequence !== sequence) return;
      setPageError(presentKnowledgeError(error));
    } finally {
      if (baseRequest.current.sequence === sequence) setLoadingBases(false);
    }
  };

  const loadDocuments = async id => {
    const sequence = documentRequest.current.sequence + 1;
    documentRequest.current.controller?.abort();
    const controller = new AbortController();
    documentRequest.current = { sequence, controller };
    if (!id) { setDocuments([]); setDocumentError(''); setLoadingDocuments(false); return; }
    setLoadingDocuments(true);
    try {
      const data = asList(await listKnowledgeDocuments(id, { signal: controller.signal }));
      if (documentRequest.current.sequence !== sequence) return;
      setDocuments(data);
      setDocumentError('');
    } catch (error) {
      if (error?.name === 'AbortError' || documentRequest.current.sequence !== sequence) return;
      setDocumentError(presentKnowledgeError(error));
    } finally {
      if (documentRequest.current.sequence === sequence) setLoadingDocuments(false);
    }
  };

  useEffect(() => {
    loadBases();
    return () => baseRequest.current.controller?.abort();
  }, []);
  useEffect(() => {
    loadDocuments(selectedId);
    return () => documentRequest.current.controller?.abort();
  }, [selectedId]);

  const createBase = async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    setCreating(true); setNotice('');
    try {
      const data = await createKnowledgeBase({
        name: String(fields.get('name') || '').trim(),
        description: String(fields.get('description') || '').trim(),
      });
      selectedIdRef.current = data.id;
      setSelectedId(data.id);
      setShowCreate(false);
      form.reset();
      await loadBases();
      setNotice('知识库已创建。');
    } catch (error) {
      setNotice(`创建失败：${presentKnowledgeError(error)}`);
    } finally {
      setCreating(false);
    }
  };

  const refreshAfterImport = async (result, sourceBaseId) => {
    const refreshes = [loadBases()];
    const stillSelected = shouldRefreshKnowledgeDocuments(sourceBaseId, selectedIdRef.current);
    if (stillSelected) {
      refreshes.push(loadDocuments(sourceBaseId));
    }
    await Promise.all(refreshes);
    if (!shouldRefreshKnowledgeDocuments(sourceBaseId, selectedIdRef.current)) return;
    if (result.status === 'ocr_required') setNotice('文件已识别为扫描版 PDF，配置 OCR 后才能写入向量索引。');
    else if (!['failed', 'error'].includes(String(result.status).toLowerCase())) setNotice('文档处理结果已更新。');
  };

  const selected = bases.find(base => base.id === selectedId);
  const selectedCanWrite = canWrite(selected);
  return <div className="extension-page">
    <div className="catalog-heading"><div><h2>知识库</h2><p className="page-subtitle">会被 Data Query Agent 的 RAG 自动检索，用于补充指标口径、业务别名和数据说明。</p></div><button type="button" className="outline-button" onClick={() => setShowCreate(open => !open)}>＋ 新建知识库</button></div>
    {showCreate && <form className="new-kb-form" onSubmit={createBase}><input required name="name" placeholder="知识库名称，例如：电商业务规则" /><input name="description" placeholder="简要说明（可选）" /><button disabled={creating}>{creating ? '创建中…' : '创建'}</button></form>}
    {notice && <p className="save-notice catalog-notice" role="status">{notice}</p>}
    {pageError && <div className="knowledge-page-error" role="alert"><p>{pageError}</p><button type="button" className="outline-button" onClick={loadBases}>重新加载</button></div>}
    <div className="knowledge-layout">
      <aside className="kb-list">{bases.map(base => <button type="button" key={base.id} className={base.id === selectedId ? 'active' : ''} onClick={() => { selectedIdRef.current = base.id; setSelectedId(base.id); }}><b>{base.name}</b><span>{base.document_count || 0} 个文档 · {canWrite(base) ? '可写' : '只读'} · 混合检索</span></button>)}</aside>
      <div className="kb-detail">{selected ? <>
        <div className="kb-detail-heading"><div><h2>{selected.name}</h2><p>{selected.description || '未填写说明'}</p></div><span className="index-badge">BGE · BM25 · Rerank</span></div>
        {selectedCanWrite
          ? <KnowledgeUploader key={selectedId} knowledgeBaseId={selectedId} onImported={refreshAfterImport} />
          : <div className="knowledge-readonly" role="note"><b>只读知识库</b><span>你可以检索和查看这里的文档，但不能向共享知识库上传内容。请新建个人知识库后导入文件。</span></div>}
        <DocumentList documents={documents} loading={loadingDocuments} error={documentError} onRetry={() => loadDocuments(selectedId)} />
      </> : !pageError && <p>{loadingBases ? '正在加载知识库…' : bases.length ? '请选择知识库。' : '还没有知识库，请先新建一个。'}</p>}</div>
    </div>
  </div>;
}
