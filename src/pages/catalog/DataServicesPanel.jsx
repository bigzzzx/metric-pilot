import { useEffect, useState } from 'react';
import { serviceRequest, serviceQueryBody } from './dataServiceApi.js';
import './DataServices.css';

export default function DataServicesPanel() {
  const [metrics, setMetrics] = useState([]), [selected, setSelected] = useState('');
  const [start, setStart] = useState(''), [end, setEnd] = useState(''), [dimensions, setDimensions] = useState([]);
  const [filters, setFilters] = useState('[]'), [result, setResult] = useState(null);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    serviceRequest('/metrics').then(items => { if (live) { setMetrics(items); setSelected(items.find(item => item.can_query)?.metric_id || ''); } })
      .catch(err => live && setError(err.message));
    return () => { live = false; };
  }, []);
  const metric = metrics.find(item => item.metric_id === selected);
  const body = metric ? serviceQueryBody(metric, start, end, dimensions) : null;
  const run = async event => {
    event.preventDefault(); setBusy(true); setError(''); setResult(null);
    try {
      const conditions = JSON.parse(filters);
      if (!Array.isArray(conditions)) throw new Error('筛选条件应为数组。');
      setResult(await serviceRequest('/metrics/query', { method: 'POST', body: JSON.stringify({ ...body, filters: conditions }) }));
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };
  return <section className="data-service-panel">
    <div className="service-intro"><div><span className="service-eyebrow">共享数据服务</span><h2>同一指标，一致口径</h2>
      <p>聊天、看板和应用共享已审核的指标定义。在这里选择指标和统计条件即可直接查询。</p></div></div>
    <form className="service-query-form" onSubmit={run}>
      <label>指标<select aria-label="指标" value={selected} required onChange={event => { setSelected(event.target.value); setDimensions([]); setFilters('[]'); setResult(null); }}>
        <option value="">选择已认证指标</option>{metrics.map(item => <option key={item.metric_id} value={item.metric_id} disabled={!item.can_query}>{item.name}{!item.can_query ? ' · 暂不可查询' : ''}</option>)}</select></label>
      <label>起始日期（含）<input type="date" value={start} required={metric?.requires_time_range} onChange={e => setStart(e.target.value)} /></label>
      <label>结束日期（不含）<input type="date" value={end} required={Boolean(start) || metric?.requires_time_range} onChange={e => setEnd(e.target.value)} /></label>
      {metric && <p className="service-form-description">{metric.description}{metric.unit ? ` · 单位：${metric.unit}` : ''}</p>}
      {metric && Object.keys(metric.dimensions).length > 0 && <fieldset className="service-dimensions"><legend>分组维度</legend>{Object.entries(metric.dimensions).map(([key, dim]) =>
        <label key={key}><input type="checkbox" checked={dimensions.includes(key)} onChange={event => setDimensions(event.target.checked ? [...dimensions, key] : dimensions.filter(value => value !== key))} />{dim.label || key}</label>)}</fieldset>}
      <details className="service-advanced"><summary>更多筛选条件</summary><p>只能筛选指标已定义的维度。例如：{`[{"dimension":"date","operator":">=","value":"2026-08-01"}]`}</p><textarea aria-label="指标筛选条件" value={filters} onChange={e => setFilters(e.target.value)} /></details>
      <div className="service-actions"><button type="submit" disabled={busy || !metric?.can_query}>{busy ? '正在查询…' : '查询指标'}</button><span>仅显示当前账号有权限的指标和数据</span></div>
    </form>
    {error && <p className="service-error" role="alert">{error}</p>}
    {result && <article className="service-result"><div className="service-result-heading"><h3>查询结果</h3><span>{result.duration_ms} ms · {result.rows.length} 行</span></div>
      <p>{result.definition}</p><div className="service-result-meta"><span>单位：{result.unit || '未维护'}</span><span>数据更新：{result.data_updated_at || '源表尚无可验证的更新时间'}</span><span>质量：{({ healthy:'通过', warning:'需关注', critical:'异常' })[result.quality?.status] || '未检测'}</span></div>
      <div className="catalog-table-wrap"><table className="catalog-data-table"><thead><tr>{Object.keys(result.rows[0] || {}).map(key => <th key={key}>{key}</th>)}</tr></thead><tbody>{result.rows.map((row, i) => <tr key={i}>{Object.entries(row).map(([key, value]) => <td key={key}>{value === null ? '无数据' : String(value)}</td>)}</tr>)}</tbody></table>{!result.rows.length && <p>该条件下没有数据。</p>}</div>
      <details><summary>查看口径版本、来源和 SQL</summary><p className="service-version">{result.metric_id} · {result.version}</p><p>{result.source_id} · {result.asset_id}</p><pre>{result.sql}</pre>{result.materialization && <p>最近重算区间：{result.materialization.range_start} 至 {result.materialization.range_end}（结束日不含）</p>}</details>
    </article>}
    {body && <details className="service-api-example"><summary>应用接入示例</summary><p>使用现有登录会话调用；指标版本变化后须重新获取目录并确认口径。</p><code>POST /api/data-services/metrics/query</code><pre>{JSON.stringify(body, null, 2)}</pre></details>}
  </section>;
}
