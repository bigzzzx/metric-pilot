import { useEffect, useRef, useState } from 'react';
import './ChatComposer.css';

const accept = '.png,.jpg,.jpeg,.webp,.pdf,.docx,.xlsx,.pptx,.html,.htm,.md,.txt,.sql,.json,.yaml,.yml,.csv';
export function AttachmentLinks({ items = [], session }) {
  return <div className="attachment-links">{items.map(file => <a key={file.id} href={`/api/conversations/${session}/attachments/${file.id}`} target="_blank" rel="noreferrer">
    {file.kind === 'image' && <img src={`/api/conversations/${session}/attachments/${file.id}`} alt={file.name} />}
    <span>{file.name}</span>
  </a>)}</div>;
}

export default function ChatComposer({ onSend, busy, session }) {
  const [value, setValue] = useState('');
  const [files, setFiles] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const input = useRef(null);
  const active = useRef(true);
  const uploadingRef = useRef(false);
  const controller = useRef(new AbortController());
  useEffect(() => () => { active.current = false; controller.current.abort(); }, []);

  const upload = async incoming => {
    if (busy || uploadingRef.current) return;
    const chosen = Array.from(incoming);
    if (files.length + chosen.length > 5) { setError('每条消息最多添加 5 个附件。'); return; }
    if (chosen.some(file => file.size > 20 * 1024 * 1024)) { setError('单个附件不能超过 20 MB。'); return; }
    uploadingRef.current = true;
    setUploading(true); setError('');
    try {
      for (const file of chosen) {
        const body = new FormData(); body.append('file', file);
        const response = await fetch(`/api/conversations/${session}/attachments/upload`, { method: 'POST', body, signal: controller.current.signal });
        const result = await response.json();
        if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : '附件上传失败。');
        if (active.current) setFiles(items => [...items, result]);
      }
    } catch (err) { if (active.current && err.name !== 'AbortError') setError(err.message); }
    finally { uploadingRef.current = false; if (active.current) setUploading(false); }
  };
  const remove = async file => {
    try {
      const response = await fetch(`/api/conversations/${session}/attachments/${file.id}`, { method: 'DELETE' });
      if (!response.ok) throw Error('移除附件失败，请重试。');
      setFiles(items => items.filter(item => item.id !== file.id));
    } catch (err) { setError(err.message); }
  };
  const send = event => {
    event?.preventDefault();
    if (busy || uploading || (!value.trim() && !files.length)) return;
    onSend(value.trim() || '请解读这些附件。', files);
    setValue(''); setFiles([]);
  };
  return <form className="home-composer" onSubmit={send}
    onDragOver={event => event.preventDefault()}
    onDrop={event => { event.preventDefault(); upload(event.dataTransfer.files); }}>
    {!!files.length && <div className="pending-attachments">{files.map(file => <div key={file.id}>
      <AttachmentLinks items={[file]} session={session} />
      <button type="button" aria-label={`移除 ${file.name}`} disabled={busy || uploading} onClick={() => remove(file)}>×</button>
    </div>)}</div>}
    <textarea value={value} onChange={event => setValue(event.target.value)}
      onPaste={event => { if (event.clipboardData.files.length) { event.preventDefault(); upload(event.clipboardData.files); } }}
      onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(); } }}
      placeholder="发消息给 Data Agent…" rows="3" />
    {error && <p className="attachment-error" role="alert">{error}</p>}
    {uploading && <p role="status">正在上传并解析附件…</p>}
    <div className="composer-toolbar">
      <input ref={input} type="file" multiple accept={accept} hidden onChange={event => { upload(event.target.files); event.target.value = ''; }} />
      <button className="attachment-add" type="button" aria-label="添加图片或文件" title="图片或文件，单个最大 20 MB" disabled={busy || uploading} onClick={() => input.current.click()}>＋</button>
      <span className="attachment-hint">附件仅用于当前会话</span>
      <span className="toolbar-spacer" />
      <button type="submit" disabled={busy || uploading || (!value.trim() && !files.length)} aria-label="发送">↑</button>
    </div>
  </form>;
}
