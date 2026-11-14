import { useEffect, useState } from 'react';

const roles = { dataset: '来源资产', value: '计算字段', required_filter: '固定过滤', time: '时间字段',
  optional_dimension: '可选维度', row_count: '行集依赖' };
export const dependencyLabel = role => roles[role] || role;
const ref = (asset, column) => [asset?.namespace, asset?.physical_name, column].filter(Boolean).join('.');

export default function MetricLineage({ metric }) {
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setResult(null); setError('');
    fetch(`/api/catalog/metrics/${encodeURIComponent(metric.id)}/lineage`, { signal: controller.signal })
      .then(async response => {
        const data = await response.json();
        if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '无法读取指标血缘');
        return data;
      }).then(data => { if (!controller.signal.aborted) setResult(data); })
      .catch(err => { if (!controller.signal.aborted) setError(err.message); });
    return () => controller.abort();
  }, [open, metric.id, metric.catalog_version]);
  return <details className="metric-lineage" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>查看指标血缘</summary>
    {error ? <p role="alert" className="catalog-error">{error}</p> : !result ? (open && <p role="status">正在读取指标血缘…</p>) :
      result.status !== 'current' ? <p>当前没有可验证的已认证依赖。请检查指标契约、底层资产状态及字段版本。</p> : <>
        <p><strong>{ref(result.asset)}</strong> → {result.display_name} · v{result.metric_version}</p>
        <p>依据已认证执行契约自动生成 · 只展示当前有权限的依赖。</p>
        <code>{result.measure_expression}</code>
        <ul className="metric-lineage-list">{result.dependencies.filter(d => d.role !== 'dataset').map((d, i) =>
          <li key={`${d.role}:${d.column}:${i}`}><b>{dependencyLabel(d.role)}</b> {d.column || '整表行集'}
            {d.operator && <> {d.operator} {JSON.stringify(d.value)}</>}{d.label && <> · {d.label}</>}
          </li>)}</ul>
        {result.upstream_edges.length > 0 && <div><b>更上游的已发布依赖</b><ul className="metric-lineage-list">
          {result.upstream_edges.map(edge => <li key={edge.id}>{ref(edge.from_asset, edge.from_column)} → {ref(edge.to_asset, edge.to_column)}
            {edge.kind === 'column_influence' && <small> · 条件影响</small>}
            {edge.observation === 'database_view_definition' && <small> · 数据库视图定义</small>}</li>)}
        </ul></div>}
      </>}
  </details>;
}
