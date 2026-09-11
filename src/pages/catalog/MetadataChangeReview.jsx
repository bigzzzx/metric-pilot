import { useEffect, useState } from 'react';
import './MetadataChangeReview.css';

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || '操作失败');
  return payload;
}

const changeLabel = value => ({
  table_added: '新增表',
  table_restored: '恢复表',
  table_changed: '表结构变化',
  table_removed: '上游已删除表',
  relationships_changed: '主外键关系变化',
}[value] || value);

function ChangeSummary({ change }) {
  const summary = change.summary || {};
  return <ul className="metadata-change-summary">
    {summary.added_columns?.length > 0 && <li>新增字段：{summary.added_columns.join('、')}</li>}
    {summary.removed_columns?.length > 0 && <li>删除字段：{summary.removed_columns.join('、')}</li>}
    {summary.type_changes?.map(item => <li key={item.column}>类型变化：{item.column}（{item.before} → {item.after}）</li>)}
    {summary.primary_key_changed && <li>主键定义发生变化</li>}
    {summary.comment_changed && <li>数据源注释发生变化</li>}
    {summary.before_count !== undefined && <li>关系数量：{summary.before_count} → {summary.after_count}</li>}
    {!summary.added_columns?.length && !summary.removed_columns?.length && !summary.type_changes?.length
      && !summary.primary_key_changed && !summary.comment_changed && summary.before_count === undefined
      && <li>发现新的技术元数据版本</li>}
  </ul>;
}

export default function MetadataChangeReview({ sourceId, onCatalogChanged = () => {} }) {
  const [runs, setRuns] = useState([]);
  const [changes, setChanges] = useState([]);
  const [approved, setApproved] = useState([]);
  const [selected, setSelected] = useState([]);
  const [reasons, setReasons] = useState({});
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');

  const load = async () => {
    if (!sourceId) return;
    try {
      const [nextRuns, nextChanges, recentApproved] = await Promise.all([
        requestJson(`/api/data-sources/${sourceId}/sync-runs?limit=8`),
        requestJson(`/api/data-sources/${sourceId}/changes?status=pending`),
        requestJson(`/api/data-sources/${sourceId}/changes?status=approved`),
      ]);
      setRuns(nextRuns);
      setChanges(nextChanges);
      setApproved(recentApproved.slice(0, 8));
      setSelected(current => current.filter(id => nextChanges.some(item => item.id === id)));
    } catch (error) {
      setNotice(`无法读取同步变更：${error.message}`);
    }
  };

  useEffect(() => { setRuns([]); setChanges([]); setApproved([]); setSelected([]); setNotice(''); load(); }, [sourceId]);

  const decide = async (change, decision) => {
    setBusy(true); setNotice('');
    try {
      const result = await requestJson(
        `/api/data-sources/${sourceId}/changes/${change.id}/decision`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ decision, reason: reasons[change.id] || '' }) },
      );
      setNotice(result.message);
      await load();
      await onCatalogChanged();
    } catch (error) {
      setNotice(`处理失败：${error.message}`);
    } finally { setBusy(false); }
  };

  const decideBatch = async decision => {
    if (!selected.length) return;
    setBusy(true); setNotice('');
    try {
      const result = await requestJson(
        `/api/data-sources/${sourceId}/changes/batch-decision`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ change_ids: selected, decision, reason: '管理员批量审核' }) },
      );
      setNotice(result.message);
      await load();
      await onCatalogChanged();
    } catch (error) {
      setNotice(`批量处理失败：${error.message}`);
    } finally { setBusy(false); }
  };

  const rollback = async change => {
    if (!window.confirm(`确定回滚“${change.entity_key}”最近一次已批准的元数据变更吗？`)) return;
    setBusy(true); setNotice('');
    try {
      const result = await requestJson(
        `/api/data-sources/${sourceId}/changes/${change.id}/rollback`, { method: 'POST' },
      );
      setNotice(result.message);
      await load();
      await onCatalogChanged();
    } catch (error) {
      setNotice(`回滚失败：${error.message}`);
    } finally { setBusy(false); }
  };

  const latest = runs[0];
  return <section className="metadata-review">
    <div className="metadata-review-heading">
      <div><h3>元数据同步与变更审核</h3><p className="page-subtitle">采集只生成快照和差异；批准后才修改目录。已发布资产的 RAG 仅刷新受影响切片。</p></div>
      <button type="button" className="outline-button" disabled={busy} onClick={load}>刷新变更</button>
    </div>
    {latest ? <div className="sync-run-summary">
      <span>快照 v{latest.snapshot_version}</span>
      <span>{latest.table_count} 张表</span>
      <span>新增 {latest.added_count}</span>
      <span>变化 {latest.changed_count}</span>
      <span>删除 {latest.removed_count}</span>
      <span>待审核 {latest.pending_count}</span>
      <time>{latest.completed_at || latest.created_at}</time>
    </div> : <p className="page-subtitle">尚未执行过版本化采集。</p>}
    {changes.length > 0 ? <>
      <div className="metadata-batch-actions">
        <label><input type="checkbox" checked={selected.length === changes.length} onChange={event => setSelected(event.target.checked ? changes.map(item => item.id) : [])}/>选择全部待审核变更</label>
        <button type="button" disabled={busy || !selected.length} onClick={() => decideBatch('approve')}>批量批准（{selected.length}）</button>
        <button type="button" className="outline-button" disabled={busy || !selected.length} onClick={() => decideBatch('reject')}>批量拒绝</button>
      </div>
      <div className="metadata-change-list">{changes.map(change => <article key={change.id}>
        <label className="metadata-change-select"><input type="checkbox" checked={selected.includes(change.id)} onChange={event => setSelected(current => event.target.checked ? [...current, change.id] : current.filter(id => id !== change.id))}/></label>
        <div className="metadata-change-main"><div><b>{change.entity_key}</b><span>{changeLabel(change.change_type)}</span></div><ChangeSummary change={change}/><input value={reasons[change.id] || ''} onChange={event => setReasons(current => ({ ...current, [change.id]: event.target.value }))} placeholder="审核备注（可选）"/></div>
        <div className="metadata-change-actions"><button type="button" disabled={busy} onClick={() => decide(change, 'approve')}>批准</button><button type="button" className="outline-button" disabled={busy} onClick={() => decide(change, 'reject')}>拒绝</button></div>
      </article>)}</div>
    </> : latest && <p className="metadata-no-change">当前没有待审核变更。已发布目录不会因单纯采集而被覆盖。</p>}
    {runs.length > 1 && <details className="sync-history"><summary>查看最近采集历史（{runs.length}）</summary>{runs.map(run => <p key={run.id}>v{run.snapshot_version} · {run.status} · {run.table_count} 张表 · 待审核 {run.pending_count} · {run.completed_at || run.created_at}</p>)}</details>}
    {approved.length > 0 && <details className="sync-history"><summary>最近已批准变更（{approved.length}）</summary>{approved.map(change => <p key={change.id}><span>{change.entity_key} · {changeLabel(change.change_type)}</span><button type="button" className="text-button" disabled={busy} onClick={() => rollback(change)}>回滚最近版本</button></p>)}</details>}
    {notice && <p className="save-notice catalog-notice" role="status">{notice}</p>}
  </section>;
}
