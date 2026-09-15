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
  const [wiki, setWiki] = useState(metric.wiki_draft?.content ? metric.wiki_draft : metric.wiki || { summary: '', content: '', related_metric_ids: [], claims: [] });
  const [claims, setClaims] = useState(JSON.stringify((metric.wiki_draft?.content ? metric.wiki_draft : metric.wiki)?.claims || [], null, 2));
  const [source, setSource] = useState({ source_type: 'business_document', title: '', source_uri: '', content: '' });
  const generate = async () => {
    setBusy(true); setNotice('');
    try {
      const result = await request(`/api/catalog/admin/metrics/${metric.id}/wiki-draft`, { method: 'POST' });
      setWiki(result); setClaims(JSON.stringify(result.claims || [], null, 2));
      setEditing(true); setNotice('草稿已生成。请逐条核对来源证据和执行契约后发布。');
    } catch (error) { setNotice(error.message); } finally { setBusy(false); }
  };
  const saveSource = async () => {
    setBusy(true); setNotice('');
    try {
      await request(`/api/catalog/admin/metrics/${metric.id}/wiki-sources`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(source),
      });
      setSource({ source_type: 'business_document', title: '', source_uri: '', content: '' });
      setNotice('原始来源已按内容哈希保存；该记录不可修改或删除。'); await onUpdated();
    } catch (error) { setNotice(error.message); } finally { setBusy(false); }
  };
  const retryIndex = async () => {
    setBusy(true); setNotice('');
    try {
      await request(`/api/catalog/admin/metrics/${metric.id}/wiki-index/retry`, { method: 'POST' });
      setNotice('已重新提交 Wiki 检索索引任务。'); await onUpdated();
    } catch (error) { setNotice(error.message); } finally { setBusy(false); }
  };
  const publish = async () => {
    setBusy(true); setNotice('');
    try {
      await request(`/api/catalog/admin/metrics/${metric.id}/publish`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_version: metric.catalog_version, aliases: aliases.split(/[、,，]/).map(v => v.trim()).filter(Boolean),
          contract: JSON.parse(contract), wiki: { summary: wiki.summary || '', content: wiki.content || '',
            related_metric_ids: wiki.related_metric_ids || [], claims: JSON.parse(claims) } }),
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
    {metric.wiki_index && <p><small>Wiki 检索索引：{metric.wiki_index.status}{metric.wiki_index.error ? ` · ${metric.wiki_index.error}` : ''}</small>
      {metric.wiki_index.status === 'failed' && <> <button className="link-button" disabled={busy} onClick={retryIndex}>重试索引</button></>}</p>}
    {!!metric.aliases?.length && <p>别名：{metric.aliases.join('、')}</p>}
    {metric.wiki?.content && <details><summary>口径 Wiki · 已审核</summary><p>{metric.wiki.summary}</p><p style={{ whiteSpace: 'pre-wrap' }}>{metric.wiki.content}</p>
      {!!metric.wiki.related_metric_ids?.length && <p>相关口径：{metric.wiki.related_metric_ids.map(id => metrics.find(m => m.id === id)?.display_name).filter(Boolean).join('、')}</p>}
      {!!metric.wiki.claims?.length && <div><strong>可追溯声明</strong>{metric.wiki.claims.map((claim, index) =>
        <p key={`${claim.source_ref}:${index}`}><span className="asset-eyebrow">{claim.claim_type}</span> {claim.content}<br />
          <small>来源：{claim.source_ref}<br />原文：{claim.evidence}</small></p>)}</div>}
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
      <label>可追溯声明（JSON）<textarea rows={10} style={{ width: '100%', fontFamily: 'monospace' }} value={claims} onChange={e => setClaims(e.target.value)} /></label>
      <small>每条声明必须包含 claim_type、content、source_ref，并复制对应来源中的原文 evidence。</small>
      <button disabled={busy} onClick={publish}>确认口径并发布</button>
      <details open>
        <summary>不可变原始来源（{metric.wiki_sources?.length || 0}）</summary>
        {(metric.wiki_sources || []).map(item => <p key={item.id}><strong>{item.title}</strong><br />
          <small>{item.id} · {item.source_type} · SHA-256 {item.content_hash}<br />{item.source_uri || '无外部地址'}</small></p>)}
        <label>来源类型<select value={source.source_type} onChange={e => setSource({ ...source, source_type: e.target.value })}>
          <option value="business_document">业务口径文档</option><option value="sql_definition">历史 SQL 定义</option>
          <option value="meeting_record">会议确认记录</option><option value="knowledge_document">知识库文档</option>
          <option value="manual">人工说明</option>
        </select></label>
        <label>来源标题<input style={{ width: '100%' }} value={source.title} onChange={e => setSource({ ...source, title: e.target.value })} /></label>
        <label>来源地址（可选）<input style={{ width: '100%' }} value={source.source_uri} onChange={e => setSource({ ...source, source_uri: e.target.value })} /></label>
        <label>原始内容<textarea rows={8} style={{ width: '100%' }} value={source.content} onChange={e => setSource({ ...source, content: e.target.value })} /></label>
        <button className="outline-button" disabled={busy || !source.title.trim() || !source.content.trim()} onClick={saveSource}>保存不可变来源</button>
      </details>
    </div>}
    {notice && <p role="status">{notice}</p>}
  </article>;
}

export default function MetricCenter({ metrics, onUpdated }) {
  return <><p className="page-subtitle">先确定指标口径，再查询数据。Wiki 解释业务差异；已认证的执行契约固定计算公式与过滤条件。</p>
    <div className="metric-grid">{metrics.map(metric => <MetricCard key={`${metric.id}:${metric.catalog_version}`} metric={metric} metrics={metrics} onUpdated={onUpdated} />)}</div></>;
}
