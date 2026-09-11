import { useEffect, useState } from 'react';
import './DataQualityPanel.css';

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || '操作失败');
  return payload;
}

const statusLabel = value => ({ completed: '检测完成', partial: '部分失败', failed: '检测失败', running: '检测中' }[value] || value);

export default function DataQualityPanel({ sourceId, enabled, onCatalogChanged = () => {} }) {
  const [runs, setRuns] = useState([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');

  const load = async () => {
    if (!sourceId) return;
    try { setRuns(await requestJson(`/api/data-sources/${sourceId}/quality/runs?limit=8`)); }
    catch (error) { setNotice(`无法读取质量检测记录：${error.message}`); }
  };

  useEffect(() => { setRuns([]); setNotice(''); load(); }, [sourceId]);

  const run = async () => {
    setBusy(true); setNotice('正在使用只读连接计算质量指标…');
    try {
      const result = await requestJson(`/api/data-sources/${sourceId}/quality/run`, { method: 'POST' });
      setNotice(`检测完成：${result.profiled_count} 张表，健康 ${result.healthy_count}，警告 ${result.warning_count}，严重 ${result.critical_count}，失败 ${result.failed_count}。`);
      await load();
      await onCatalogChanged();
    } catch (error) {
      setNotice(`质量检测失败：${error.message}`);
      await load();
    } finally { setBusy(false); }
  };

  const latest = runs[0];
  return <section className="quality-run-panel">
    <div className="quality-run-heading">
      <div><h3>真实数据质量检测</h3><p className="page-subtitle">使用只读聚合查询检查行数、空值、主键重复和时间新鲜度；不保存原始业务值。</p></div>
      <button type="button" disabled={busy || !enabled} onClick={run}>{busy ? '检测中…' : '运行质量检测'}</button>
    </div>
    {latest ? <div className="quality-run-summary">
      <b>{statusLabel(latest.status)}</b><span>{latest.asset_count} 张表</span>
      <span className="healthy">健康 {latest.healthy_count}</span><span className="warning">警告 {latest.warning_count}</span>
      <span className="critical">严重 {latest.critical_count}</span><span>失败 {latest.failed_count}</span>
      <time>{latest.completed_at || latest.created_at}</time>
    </div> : <p className="page-subtitle">尚未运行真实质量检测。</p>}
    {runs.length > 1 && <details className="quality-run-history"><summary>最近检测记录（{runs.length}）</summary>{runs.map(item => <p key={item.id}><span>{item.created_at} · {statusLabel(item.status)}</span><span>健康 {item.healthy_count} / 警告 {item.warning_count} / 严重 {item.critical_count} / 失败 {item.failed_count}</span></p>)}</details>}
    {notice && <p className="save-notice catalog-notice" role="status">{notice}</p>}
  </section>;
}
