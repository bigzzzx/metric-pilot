import { useEffect, useMemo, useState } from 'react';
import DataSourceAdminPanel from './DataSourceAdminPanel.jsx';

const tabs = [
  ['assets', '数据资产'], ['metrics', '指标中心'], ['terms', '业务术语'], ['lineage', '数据血缘'], ['sources', '数据源'],
];

const label = value => ({ ODS: 'ODS · 贴源层', DWD: 'DWD · 明细层', DIM: 'DIM · 维度层', DWS: 'DWS · 汇总层', ADS: 'ADS · 应用层', raw: '贴源', detail: '明细', summary: '汇总', application: '应用', refined: '精炼层', internal: '内部', sensitive: '敏感', certified: '已认证', draft: '待审核', published: '已发布' }[value] || value || '—');

async function fetchJson(url) {
  const response = await fetch(url);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || '加载失败');
  return data;
}

function Tags({ values = [] }) { return <span className="catalog-tags">{values.map(value => <i key={value}>{label(value)}</i>)}</span>; }

function AssetDetail({ assetId, onClose }) {
  const [asset, setAsset] = useState(null), [sample, setSample] = useState(null), [lineage, setLineage] = useState(null), [tab, setTab] = useState('overview'), [error, setError] = useState('');
  useEffect(() => { let live = true; setAsset(null); setSample(null); setLineage(null); setError(''); fetchJson(`/api/catalog/assets/${assetId}`).then(data => live && setAsset(data)).catch(err => live && setError(err.message)); return () => { live = false; }; }, [assetId]);
  const loadSample = () => fetchJson(`/api/catalog/assets/${assetId}/sample`).then(setSample).catch(error => setError(error.message));
  const loadLineage = () => fetchJson(`/api/catalog/assets/${assetId}/lineage`).then(setLineage).catch(error => setError(error.message));
  useEffect(() => { if (tab === 'sample' && !sample) loadSample(); if (tab === 'lineage' && !lineage) loadLineage(); }, [tab]);
  if (error) return <section className="catalog-detail"><button className="text-button" onClick={onClose}>← 返回资产列表</button><p className="catalog-error">{error}</p></section>;
  if (!asset) return <section className="catalog-detail"><p className="page-subtitle">正在加载资产详情…</p></section>;
  const detailTabs = asset.publish_status === 'published' ? [['overview','概览'],['columns','字段'],['sample','样例数据'],['relations','表关联'],['lineage','数据血缘'],['metrics','指标'],['quality','质量']] : [['overview','概览'],['columns','字段'],['relations','表关联']];
  return <section className="catalog-detail">
    <button className="text-button" onClick={onClose}>← 返回资产列表</button>
    <div className="asset-detail-heading"><div><div className="asset-eyebrow">{asset.asset_type} · {asset.physical_name}</div><h2>{asset.display_name}</h2><p>{asset.description}</p><Tags values={[asset.warehouse_layer, asset.normalized_stage, asset.sensitivity, asset.certification_status]} /></div><div className="asset-quality"><b>{asset.quality?.score ? `${Math.round(asset.quality.score * 100)}%` : '—'}</b><span>质量评分 · {asset.quality?.status || '未检测'}</span></div></div>
    <div className="catalog-tabs detail-tabs">{detailTabs.map(([id,name]) => <button key={id} className={tab===id?'active':''} onClick={() => setTab(id)}>{name}</button>)}</div>
    {tab === 'overview' && <div className="detail-grid"><article><h3>业务语义</h3><dl><dt>业务域</dt><dd>{asset.domain_id}</dd><dt>数据粒度</dt><dd>{asset.data_grain || '未维护'}</dd><dt>主键</dt><dd><code>{asset.primary_key || '未维护'}</code></dd><dt>数据源</dt><dd>{asset.source_id}</dd></dl></article><article><h3>治理状态</h3><dl><dt>发布状态</dt><dd>{asset.publish_status}</dd><dt>可执行查询</dt><dd>{asset.can_query ? '是（列级规则生效）' : '否'}</dd><dt>敏感级别</dt><dd>{label(asset.sensitivity)}</dd><dt>认证</dt><dd>{label(asset.certification_status)}</dd></dl></article></div>}
    {tab === 'columns' && <div className="catalog-table-wrap"><table className="catalog-data-table"><thead><tr><th>字段</th><th>类型</th><th>语义角色</th><th>说明</th><th>敏感级别</th></tr></thead><tbody>{asset.columns.map(column => <tr key={column.id}><td><b>{column.display_name}</b><code>{column.physical_name}</code></td><td>{column.data_type}</td><td>{column.semantic_role}</td><td>{column.description}<Tags values={column.aliases} /></td><td><span className={`sensitivity ${column.sensitivity}`}>{label(column.sensitivity)}</span>{column.masked_in_samples && <small> 样例脱敏</small>}</td></tr>)}</tbody></table></div>}
    {tab === 'sample' && <div><p className="sample-notice">{sample?.notice || '按需加载最多 5 条样例；敏感字段会在服务端脱敏。'}</p>{!sample ? <p className="page-subtitle">正在获取脱敏样例…</p> : <div className="catalog-table-wrap"><table className="catalog-data-table"><thead><tr>{sample.columns.map(column => <th key={column}>{column}</th>)}</tr></thead><tbody>{sample.rows.map((row,index) => <tr key={index}>{sample.columns.map(column => <td key={column}>{String(row[column] ?? '')}</td>)}</tr>)}</tbody></table></div>}</div>}
    {tab === 'relations' && <div className="relation-list">{asset.relationships.filter(item => item.visible).length ? asset.relationships.filter(item => item.visible).map(item => <article key={item.id}><b>{item.from_asset?.physical_name}.{item.from_column}</b><span> {item.recommended_join} · {item.cardinality} → </span><b>{item.to_asset?.physical_name}.{item.to_column}</b><p>{item.description}</p></article>) : <p className="page-subtitle">当前资产没有已发布的表关联。</p>}</div>}
    {tab === 'lineage' && <div>{!lineage ? <p className="page-subtitle">正在加载有向血缘…</p> : <LineageGraph data={lineage} />}</div>}
    {tab === 'metrics' && <div className="metric-grid">{asset.metrics.map(metric => <article key={metric.id}><b>{metric.display_name}</b><p>{metric.description}</p><code>{metric.formula}</code><Tags values={[metric.certification_status]} /></article>)}</div>}
    {tab === 'quality' && <div className="quality-list">{asset.quality_history.map(item => <article key={item.observed_at}><b>{Math.round(item.score * 100)}%</b><span>{item.status} · 完整性 {Math.round((item.completeness || 0) * 100)}% · 有效性 {Math.round((item.validity || 0) * 100)}%</span><small>{item.observed_at}</small></article>)}</div>}
  </section>;
}

function LineageGraph({ data }) {
  const nodes = data.nodes || []; const positions = nodes.map((node, index) => ({ ...node, x: 28 + (index % 3) * 260, y: 40 + Math.floor(index / 3) * 122 })); const byId = Object.fromEntries(positions.map(node => [node.id,node]));
  return <div className="lineage-canvas"><p className="page-subtitle">有向图：源表 → 同步/转换任务 → 已发布资产。表关联在“表关联”页签单独展示。</p><svg viewBox="0 0 820 350" role="img" aria-label="数据血缘有向图"><defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#7c8999" /></marker></defs>{data.edges.map(edge => { const from=byId[edge.from_node_id], to=byId[edge.to_node_id]; if(!from||!to) return null; return <line key={edge.id} x1={from.x+178} y1={from.y+30} x2={to.x} y2={to.y+30} markerEnd="url(#arrow)" />; })}{positions.map(node => <g key={node.id} transform={`translate(${node.x},${node.y})`}><rect width="176" height="60" rx="9" className={node.restricted?'restricted-node':''}/><text x="12" y="24">{node.node_type}</text><text x="12" y="43" className="lineage-node-title">{node.display_name}</text></g>)}</svg></div>;
}

export default function CatalogPage({ isCatalogAdmin = false }) {
  const [tab, setTab] = useState('assets'); const [overview, setOverview] = useState(null); const [selected, setSelected] = useState(null); const [q, setQ] = useState(''); const [filters, setFilters] = useState({ domain_id:'', layer:'', sensitivity:'' }); const [error, setError] = useState('');
  const load = () => { setError(''); return fetchJson('/api/catalog/overview').then(setOverview).catch(err => setError(err.message)); };
  useEffect(() => { load(); }, []);
  const assets = useMemo(() => (overview?.assets || []).filter(asset => (!q || `${asset.display_name} ${asset.physical_name} ${asset.description} ${(asset.aliases||[]).join(' ')}`.toLowerCase().includes(q.toLowerCase())) && (!filters.domain_id || asset.domain_id===filters.domain_id) && (!filters.layer || asset.warehouse_layer===filters.layer) && (!filters.sensitivity || asset.sensitivity===filters.sensitivity)), [overview,q,filters]);  const visibleTabs = isCatalogAdmin ? tabs : tabs.filter(([id]) => id !== 'sources');

  if (selected) return <section className="page catalog-page-new"><AssetDetail assetId={selected} onClose={() => setSelected(null)} /></section>;
  return <section className="page catalog-page-new"><div className="catalog-header"><div><h1>数据目录</h1><p className="page-subtitle">{isCatalogAdmin ? '管理员可查看已发布资产和待审核草稿；普通用户只会看到已发布且有权限的资产。' : '面向业务域、数仓分层和权限治理的数据资产目录；仅显示你有发现权限的已发布资产。'}</p></div><button className="outline-button" onClick={load}>刷新目录</button></div><div className="catalog-tabs">{visibleTabs.map(([id,name]) => <button key={id} className={tab===id?'active':''} onClick={() => setTab(id)}>{name}</button>)}</div>{error && <p className="catalog-error">{error}</p>}{!overview ? <p className="page-subtitle">正在加载已授权的数据目录…</p> : <>
    {tab === 'assets' && <><div className="catalog-filters"><input value={q} onChange={event => setQ(event.target.value)} placeholder="搜索表名、业务名称、字段说明或别名"/><select value={filters.domain_id} onChange={event=>setFilters({...filters,domain_id:event.target.value})}><option value="">全部业务域</option>{overview.domains.map(domain=><option key={domain.id} value={domain.id}>{domain.display_name}</option>)}</select><select value={filters.layer} onChange={event=>setFilters({...filters,layer:event.target.value})}><option value="">全部数仓层</option>{overview.facets.layers.map(layer=><option key={layer}>{layer}</option>)}</select><select value={filters.sensitivity} onChange={event=>setFilters({...filters,sensitivity:event.target.value})}><option value="">全部敏感级别</option>{overview.facets.sensitivities.map(item=><option key={item}>{item}</option>)}</select></div><div className="domain-summary">{overview.domains.map(domain => <article key={domain.id}><b>{domain.display_name}</b><span>{domain.asset_count} 个已发布资产{isCatalogAdmin && domain.draft_asset_count ? ` · ${domain.draft_asset_count} 个待审核` : ''}</span><p>{domain.description}</p></article>)}</div><div className="asset-card-grid">{assets.map(asset => <button className={`asset-card ${asset.publish_status === 'draft' ? 'draft' : ''}`} key={asset.id} onClick={() => setSelected(asset.id)}><div><span className="asset-eyebrow">{asset.asset_type} · {asset.namespace ? `${asset.namespace}.` : ''}{asset.physical_name}</span><h3>{asset.display_name}</h3><p>{asset.description}</p></div><Tags values={[asset.warehouse_layer,asset.normalized_stage,asset.sensitivity,asset.certification_status,asset.publish_status]} /><footer><span>{asset.data_grain || '未维护粒度'}</span><span>{asset.publish_status === 'draft' ? '待审核发布' : asset.can_query?'可查询':'仅可查看元数据'}</span></footer></button>)}</div>{!assets.length && <p className="page-subtitle">没有符合条件且已授权的资产。</p>}</>}
    {tab === 'metrics' && <div className="metric-grid">{overview.metrics.map(metric => <article key={metric.id}><span className="asset-eyebrow">metric · {metric.name}</span><h3>{metric.display_name}</h3><p>{metric.description}</p><code>{metric.formula}</code><p className="metric-definition">{metric.definition_sql}</p><Tags values={[metric.certification_status,metric.sensitivity]} /></article>)}</div>}
    {tab === 'terms' && <div className="term-grid">{overview.terms.map(term => <article key={term.id}><b>{term.display_name}</b><code>{term.term}</code><p>{term.definition}</p><Tags values={term.aliases} /></article>)}</div>}
    {tab === 'lineage' && <div className="catalog-empty"><h2>选择一张数据资产查看血缘</h2><p>数据血缘采用左到右的有向图，展示上游源表、同步/转换任务和下游已发布资产；表之间的 Join 关系则在资产详情中单独维护。</p><button onClick={() => setTab('assets')}>浏览数据资产</button></div>}
    {tab === 'sources' && isCatalogAdmin && <DataSourceAdminPanel onCatalogChanged={load} />}
  </>}</section>;
}
