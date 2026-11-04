import { useEffect, useState } from 'react';
import ToolApprovalCard from '../../components/chat/ToolApprovalCard.jsx';
import { serviceRequest } from './dataServiceApi.js';
import './DataServices.css';

const statusLabels = { running:'运行中', succeeded:'成功', failed:'失败', needs_reconciliation:'待核对元数据' };
const fieldLabels = { time_column:'订单日期', amount_column:'订单金额', status_column:'订单状态', order_id_column:'订单唯一标识' };
const candidates = { time_column:['order_date','order_time','created_at'], amount_column:['amount','order_amount','paid_amount'], status_column:['status','order_status'], order_id_column:['order_id','id'] };

export default function ProductionPanel({ assets = [], isAdmin, onCatalogChanged }) {
  const [jobs, setJobs] = useState([]), [runs, setRuns] = useState([]), [pending, setPending] = useState([]);
  const [jobId, setJobId] = useState(''), [start, setStart] = useState(''), [end, setEnd] = useState('');
  const [sourceId, setSourceId] = useState(''), [columns, setColumns] = useState([]);
  const [draft, setDraft] = useState({ name:'', paid_value:'PAID', writer_env:'', target_namespace:'' });
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const source = assets.find(asset => asset.id === sourceId);
  const refresh = async () => {
    const [items, history] = await Promise.all([serviceRequest('/production/jobs'), serviceRequest('/production/runs')]);
    setJobs(items); setRuns(history); setJobId(current => items.some(item => item.id === current) ? current : items.find(item => item.enabled)?.id || '');
  };
  useEffect(() => {
    refresh().catch(err => setError(err.message));
    fetch('/api/tool-approvals?status=pending').then(res => res.ok ? res.json() : []).then(items => setPending(items.filter(item => item.tool_id === 'etl_execute'))).catch(() => {});
  }, []);
  useEffect(() => {
    if (!runs.some(item => item.status === 'running')) return undefined;
    const timer = setInterval(() => refresh().catch(() => {}), 3000);
    return () => clearInterval(timer);
  }, [runs]);
  useEffect(() => {
    if (!sourceId) { setColumns([]); return undefined; }
    let live = true; setColumns([]);
    fetch(`/api/catalog/assets/${encodeURIComponent(sourceId)}`).then(async response => {
      if (!response.ok) throw new Error('无法读取该资产的字段。');
      return response.json();
    }).then(asset => {
      if (!live) return;
      const fields = asset.columns.filter(column => !['sensitive','restricted'].includes(column.sensitivity));
      setColumns(fields);
      setDraft(current => ({ ...current, ...Object.fromEntries(Object.entries(candidates).map(([key, names]) => [key, names.find(name => fields.some(column => column.physical_name === name)) || ''])) }));
    }).catch(err => live && setError(err.message));
    return () => { live = false; };
  }, [sourceId]);
  const register = async event => {
    event.preventDefault(); setBusy(true); setError(''); setNotice('');
    try { const item = await serviceRequest('/production/jobs', { method:'POST', body:JSON.stringify({ ...draft, source_asset_id:sourceId }) }); await refresh(); setJobId(item.id); setNotice('任务已登记，可以选择日期发起运行。'); }
    catch (err) { setError(err.message); } finally { setBusy(false); }
  };
  const requestRun = async event => {
    event.preventDefault(); setBusy(true); setError(''); setNotice('');
    try {
      const response = await serviceRequest(`/production/jobs/${jobId}/runs`, { method:'POST', body:JSON.stringify({ start_date:start, end_date:end, request_key:crypto.randomUUID() }) });
      if (response.approval) setPending(current => [...current, response.approval]);
      else throw new Error(response.error?.message || '未能创建审批，请刷新后重试。');
      setNotice('运行已暂停，批准本次操作后才会写入汇总数据。');
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  };
  const resolved = async payload => {
    if (payload?.tool_result) {
      const result = payload.tool_result;
      setNotice(result.status === 'success' ? '运行完成。新汇总资产请在“数据资产”中审核发布。' : result.status === 'rejected' ? '本次操作已拒绝，未执行数据写入。' : '');
      setError(result.error?.message && result.status !== 'rejected' ? result.error.message : '');
    }
    await refresh(); await onCatalogChanged?.();
  };
  const toggle = async job => {
    try { await serviceRequest(`/production/jobs/${job.id}`, { method:'PATCH', body:JSON.stringify({ enabled:!job.enabled }) }); await refresh(); }
    catch (err) { setError(err.message); }
  };
  const reconcile = async run => {
    try { await serviceRequest(`/production/runs/${run.id}/reconcile`, { method:'POST' }); await resolved(); }
    catch (err) { setError(err.message); }
  };
  return <section className="data-service-panel">
    <div className="service-intro"><div><span className="service-eyebrow">数据生产</span><h2>订单明细 → 已支付日汇总</h2><p>选择已登记任务，按日期区间重算。每次运行需要批准，首次生成的资产需审核后才能查询。</p></div><button className="outline-button" onClick={() => refresh().catch(err => setError(err.message))}>刷新记录</button></div>
    {isAdmin && <details className="production-register"><summary>登记日汇总任务</summary><form onSubmit={register} className="service-query-form">
      <label>任务名称<input required maxLength={120} value={draft.name} onChange={e => setDraft({ ...draft, name:e.target.value })} placeholder="例如：电商已支付订单日汇总" /></label>
      <label className="service-full-width">输入数据资产<select aria-label="输入数据资产" required value={sourceId} onChange={e => { setSourceId(e.target.value); setDraft(current => ({ ...current, writer_env:'', target_namespace:'' })); }}><option value="">选择已发布的订单明细表</option>{assets.filter(asset => asset.can_query && asset.publish_status === 'published').map(asset => <option key={asset.id} value={asset.id}>{asset.display_name} · {asset.namespace ? `${asset.namespace}.` : ''}{asset.physical_name} · {asset.source_id}</option>)}</select></label>
      {Object.entries(fieldLabels).map(([key, title]) => <label key={key}>{title}<select aria-label={title} required value={draft[key] || ''} onChange={e => setDraft({ ...draft, [key]:e.target.value })}><option value="">选择字段</option>{columns.map(column => <option key={column.id} value={column.physical_name}>{column.display_name} · {column.physical_name}</option>)}</select></label>)}
      <label>已支付状态值<input required value={draft.paid_value} onChange={e => setDraft({ ...draft, paid_value:e.target.value })} /></label>
      {source && source.source_id !== 'local-ecommerce-demo' && <><label>汇总表 Schema<input value={draft.target_namespace} placeholder="留空使用源表 Schema" onChange={e => setDraft({ ...draft, target_namespace:e.target.value })} /></label><label className="service-full-width">服务端写入凭据环境变量名<input required value={draft.writer_env} placeholder="例如：METRIC_PILOT_PRODUCTION_DSN" onChange={e => setDraft({ ...draft, writer_env:e.target.value })} /><small>填写环境变量名称；数据库密码由管理员在服务端配置。</small></label></>}
      <div className="service-actions"><button disabled={busy || !sourceId} type="submit">登记任务</button></div>
    </form></details>}
    {jobs.length ? <><form className="service-query-form" onSubmit={requestRun}><label>已登记任务<select aria-label="已登记任务" value={jobId} required onChange={e => setJobId(e.target.value)}><option value="">选择任务</option>{jobs.map(job => <option key={job.id} value={job.id} disabled={!job.enabled}>{job.name}{!job.enabled ? ' · 已停用' : ''}</option>)}</select></label><label>起始日期（含）<input type="date" required value={start} onChange={e => setStart(e.target.value)} /></label><label>结束日期（不含）<input type="date" required value={end} onChange={e => setEnd(e.target.value)} /></label><div className="service-actions"><button disabled={busy || !jobs.find(job => job.id === jobId)?.can_execute} type="submit">申请运行</button><span>需要 etl.execute 权限及本次批准</span></div></form>
      {isAdmin && <div className="production-job-list">{jobs.map(job => <div key={job.id}><span>{job.name} · {job.enabled ? '已启用' : '已停用'}</span><button className="text-button" onClick={() => toggle(job)}>{job.enabled ? '停用' : '启用'}</button></div>)}</div>}</> : <p className="service-empty">尚未登记生产任务。管理员可先选择订单明细表登记日汇总任务。</p>}
    {notice && <p className="service-notice" role="status">{notice}</p>}{error && <p className="service-error" role="alert">{error}</p>}
    {pending.map(approval => <ToolApprovalCard key={approval.approval_id || approval.id} approval={{ ...approval, tool_name:'数据生产任务' }} onResolved={resolved} />)}
    <h3>运行记录</h3>{!runs.length && <p className="service-empty">批准后开始执行，运行步骤和结果会保存在这里。</p>}
    <div className="production-runs">{runs.map(run => <article key={run.id}><div className="service-result-heading"><strong>{jobs.find(job => job.id === run.job_id)?.name || '生产任务'}</strong><span className={`production-status ${run.status}`}>{statusLabels[run.status] || run.status}</span></div><p>{run.start_date} 至 {run.end_date}（结束日不含） · {run.started_at}</p>{run.result?.days !== undefined && <p>{run.result.days} 个日期 · {run.result.orders} 笔订单 · 金额 {run.result.amount}</p>}{run.error_message && <p className="service-error">{run.error_message}</p>}
      {isAdmin && run.status === 'needs_reconciliation' && <button className="outline-button" onClick={() => reconcile(run)}>恢复元数据登记</button>}
      <details><summary>查看步骤和执行记录</summary><ol>{run.steps.map((step, index) => <li key={index}>{step.name} · {statusLabels[step.status] || step.status}{step.code && ` · ${step.code}`}</li>)}</ol><p className="service-version">运行 ID：{run.id}</p>{run.result.sql_template && <pre>{run.result.sql_template}</pre>}</details></article>)}</div>
  </section>;
}
