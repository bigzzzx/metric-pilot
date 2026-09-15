import { useState } from 'react';

async function request(url, options) {
  const response = await fetch(url, options);
  const body = await response.json();
  if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || body));
  return body;
}

function MetricCard({ metric, metrics, onUpdated }) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [aliases, setAliases] = useState((metric.aliases || []).join('、'));
  const [contract, setContract] = useState(JSON.stringify(metric.contract || {}, null, 2));
  const [wiki, setWiki] = useState(metric.wiki_draft?.content ? metric.wiki_draft : metric.wiki || { summary: '', content: '', related_metric_ids: [] });
  const generate = async () => {
    setBusy(true); setNotice('');
    try {
      const result = await request(`/api/catalog/admin/metrics/${metric.id}/wiki-draft`, { method: 'POST' });
      setWiki(result); setEditing(true); setNotice('草稿已生成。请核对原始口径和执行契约后发布。');
    } catch (error) { setNotice(error.message); } finally { setBusy(false); }
  };
  const publish = async () => {
    setBusy(true); setNotice('');
    try {
      await request(`/api/catalog/admin/metrics/${metric.id}/publish`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_version: metric.catalog_version, aliases: aliases.split(/[、,，]/).map(v => v.trim()).filter(Boolean),
          contract: JSON.parse(contract), wiki: { summary: wiki.summary || '', content: wiki.content || '', related_metric_ids: wiki.related_metric_ids || [] } }),
      });
      setEditing(false); setNotice('口径已审核发布。'); await onUpdated();
    } catch (error) { setNotice(error.message); } finally { setBusy(false); }
  };
  return <article>
    <span className="asset-eyebrow">{metric.name} · v{metric.catalog_version}</span>
    <h3>{metric.display_name}</h3><p>{metric.description}</p>
    <code>{metric.definition_sql}</code>
    <p>{metric.contract?.status !== 'disabled' && metric.contract?.asset_id && metric.certification_status === 'certified' ? '已认证 · 支持指标查询' : '待完善执行口径'}</p>
    {metric.contract?.asset_id && <small>来源：{metric.contract.source_id} · {[metric.contract.namespace, metric.contract.table].filter(Boolean).join('.')}<br />时间字段：{metric.contract.time_column || '未配置'}</small>}
    {!!metric.aliases?.length && <p>别名：{metric.aliases.join('、')}</p>}
    {metric.wiki?.content && <details><summary>口径 Wiki · 已审核</summary><p>{metric.wiki.summary}</p><p style={{ whiteSpace: 'pre-wrap' }}>{metric.wiki.content}</p>
      {!!metric.wiki.related_metric_ids?.length && <p>相关口径：{metric.wiki.related_metric_ids.map(id => metrics.find(m => m.id === id)?.display_name).filter(Boolean).join('、')}</p>}
      <small>依据：{(metric.wiki.source_refs || []).join('、')}</small>
    </details>}
    {metric.can_manage && <div><button className="outline-button" disabled={busy} onClick={() => setEditing(!editing)}>审核口径</button>{' '}
      <button className="outline-button" disabled={busy} onClick={generate}>{busy ? '处理中…' : '生成 Wiki 草稿'}</button></div>}
    {editing && metric.can_manage && <div style={{ display: 'grid', gap: 12, marginTop: 12 }}>
      <label>指标别名<input style={{ width: '100%' }} value={aliases} onChange={e => setAliases(e.target.value)} /></label>
      <label>执行契约（JSON）<textarea aria-label="执行契约" rows={14} style={{ width: '100%', fontFamily: 'monospace' }} value={contract} onChange={e => setContract(e.target.value)} /></label>
      <small>契约需绑定已发布资产，并明确聚合表达式、时间字段、固定过滤条件和允许分组维度。</small>
      <label>Wiki 摘要<input style={{ width: '100%' }} value={wiki.summary || ''} onChange={e => setWiki({ ...wiki, summary: e.target.value })} /></label>
      <label>Wiki 正文<textarea rows={8} style={{ width: '100%' }} value={wiki.content || ''} onChange={e => setWiki({ ...wiki, content: e.target.value })} /></label>
      <label>关联指标 ID（逗号分隔）<input style={{ width: '100%' }} value={(wiki.related_metric_ids || []).join(',')} onChange={e => setWiki({ ...wiki, related_metric_ids: e.target.value.split(',').map(v => v.trim()).filter(Boolean) })} /></label>
      <button disabled={busy} onClick={publish}>确认口径并发布</button>
    </div>}
    {notice && <p role="status">{notice}</p>}
  </article>;
}

export default function MetricCenter({ metrics, onUpdated }) {
  return <><p className="page-subtitle">先确定指标口径，再查询数据。Wiki 解释业务差异；已认证的执行契约固定计算公式与过滤条件。</p>
    <div className="metric-grid">{metrics.map(metric => <MetricCard key={`${metric.id}:${metric.catalog_version}`} metric={metric} metrics={metrics} onUpdated={onUpdated} />)}</div></>;
}
