import { useState } from 'react';

/** Server-side batch publication validates versions and rebuilds RAG only once. */
export default function BatchPublication({ assets, onUpdated }) {
  const [selected, setSelected] = useState([]), [results, setResults] = useState([]), [busy, setBusy] = useState(false);
  const drafts = assets.filter(asset => asset.publish_status === 'draft');
  const chosen = drafts.filter(asset => selected.includes(asset.id));
  const publish = async () => {
    setBusy(true); setResults([]);
    try {
      const response = await fetch('/api/catalog/admin/assets/batch-publish', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: chosen.map(asset => ({ asset_id: asset.id, version: asset.catalog_version })) }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '批量发布失败');
      const names = Object.fromEntries(chosen.map(asset => [asset.id, asset.display_name]));
      const outcomes = (data.results || []).map(result => ({ ...result, id: result.asset_id, name: names[result.asset_id] || result.asset_id }));
      setResults(outcomes);
      setSelected(outcomes.filter(result => result.status !== 'published').map(result => result.id));
      await onUpdated();
    } catch (error) {
      setResults([{ id: 'request', name: '批量发布', status: 'failed', message: `请求未完成，请刷新确认状态：${error.message}` }]);
    } finally { setBusy(false); }
  };
  return <details className="batch-publication"><summary>批量发布已核对的草稿（{drafts.length}）</summary>
    <p className="page-subtitle">先逐表核对元数据，再勾选发布。缺少说明或粒度的表会保留为草稿，并显示原因。</p>
    <button className="outline-button" disabled={busy} onClick={() => setSelected(selected.length ? [] : drafts.map(a => a.id))}>{selected.length ? '取消选择' : '选择当前列表草稿'}</button>
    <div className="batch-choices">{drafts.map(asset => <label key={asset.id}><input type="checkbox" disabled={busy} checked={selected.includes(asset.id)} onChange={e => setSelected(current => e.target.checked ? [...current,asset.id] : current.filter(id => id !== asset.id))}/><span>{asset.namespace}.{asset.physical_name} · {asset.display_name}</span></label>)}</div>
    <button disabled={busy || !chosen.length} onClick={publish}>{busy ? '正在逐表发布…' : `发布所选 ${chosen.length} 个草稿`}</button>
    <ul aria-live="polite">{results.map(result => <li className={result.status === 'published' ? 'success' : 'failed'} key={result.id}>{result.name}：{result.message}</li>)}</ul>
  </details>;
}
