import { useEffect, useMemo, useState } from 'react';
import './LineageEvidence.css';

const names = {
  draft:'待审核',approved:'已批准，待发布',published:'已发布',rejected:'已拒绝',withdrawn:'已撤回',stale:'已失效',
  parsed:'解析完成',partial:'部分解析',unresolved:'未能解析',awaiting_model:'待模型提取',needs_assets:'需要绑定资产',
  model_unavailable:'模型不可用，未生成关系',no_candidates:'未发现有证据的候选',evidence_rejected:'引用未通过校验',
  design:'解析设计（不代表执行）',query_observed:'成功查询使用',run_observed:'平台成功运行',run_failed:'失败/回滚运行',
  external_reported:'外部成功上报（未内部验证）',document_suggested:'文档模型建议',database_declared:'数据库约束声明',catalog_declared:'历史目录声明（非数据库观察）',catalog_declaration:'历史目录声明',
  join:'表关联',table_transform:'表级转换',column_value:'字段值来源',column_influence:'条件/分组影响',
  sql_import:'SQL 导入',etl_definition:'ETL 定义',business_document:'业务文档',production_run:'生产运行',
  query_execution:'查询执行',external_event:'外部事件',database_constraint:'数据库约束',
  demo_seed:'演示预置',database_or_legacy_declaration:'数据库或历史声明',schema_validated:'Schema 已校验',
  type_mismatch:'类型不兼容',active:'有效',invalidated:'已失效',sampled:'已采样',unknown:'未知',unspecified:'未指定',
};
export const lineageLabel = value => names[value] || value || '—';
const enc = encodeURIComponent;
const base = '/api/catalog/evidence';

async function request(url, data, method='POST') {
  const options = data === undefined ? {} : data instanceof FormData ? {method,body:data} : {method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data)};
  const response = await fetch(url,options);
  const result = await response.json().catch(()=>({}));
  if (!response.ok) throw new Error(typeof result.detail==='string' ? result.detail : JSON.stringify(result.detail || '请求失败'));
  return result;
}

function Endpoint({ asset, column }) {
  return <span className="lineage-endpoint"><small>{asset?.source_id}</small><strong>{[asset?.namespace,asset?.physical_name,column].filter(Boolean).join('.')}</strong></span>;
}

export function FieldLineagePanel({ asset, refreshKey=0 }) {
  const [column,setColumn] = useState(''),[direction,setDirection] = useState('upstream'),[influences,setInfluences] = useState(false);
  const [result,setResult] = useState(null),[error,setError] = useState('');
  useEffect(()=>{
    let live=true; setResult(null); setError('');
    const qs = new URLSearchParams({column,direction,hops:'3',include_influences:String(influences)});
    request(`/api/catalog/assets/${enc(asset.id)}/field-lineage?${qs}`).then(data=>live&&setResult(data)).catch(e=>live&&setError(e.message));
    return ()=>{live=false;};
  },[asset.id,column,direction,influences,refreshKey]);
  return <section className="field-lineage-panel">
    <div className="evidence-heading"><div><h3>已发布的字段血缘与影响</h3><p>表关联不等于数据产出依赖；设计定义和运行事实分别标识。</p></div></div>
    <div className="evidence-toolbar">
      <label>字段<select aria-label="血缘字段" value={column} onChange={e=>setColumn(e.target.value)}><option value="">整表转换依赖</option>{(asset.columns||[]).filter(c=>!['sensitive','restricted'].includes(c.sensitivity)).map(c=><option key={c.physical_name} value={c.physical_name}>{c.physical_name}</option>)}</select></label>
      <label>方向<select aria-label="血缘方向" value={direction} onChange={e=>setDirection(e.target.value)}><option value="upstream">上游来源</option><option value="downstream">下游影响</option></select></label>
      {column && <label className="evidence-checkbox"><input type="checkbox" checked={influences} onChange={e=>setInfluences(e.target.checked)}/>包括过滤 / 分组等间接影响</label>}
    </div>
    {error && <p className="catalog-error" role="alert">{error}</p>}
    {result ? <><p className="evidence-note">{result.edges.length} 条依赖 · 最多 3 跳 · 仅当前有权限且已审核发布的证据</p>
      {!result.edges.length && <p className="evidence-empty">暂无已发布依赖。不代表业务中不存在关联，可由管理员导入证据并审核。</p>}
      {result.edges.map(edge=><article className="field-lineage-edge" key={edge.id}>
        <div className="evidence-flow"><Endpoint asset={edge.from_asset} column={edge.from_column}/><span aria-hidden="true">→</span><Endpoint asset={edge.to_asset} column={edge.to_column}/></div>
        <div className="evidence-badges"><span>{lineageLabel(edge.kind)}</span><span>{lineageLabel(edge.observation)}</span>{edge.context.influence_role && <span>{edge.context.influence_role}</span>}</div>
        <code>{edge.expression}</code>
        <details><summary>表达式、过滤、分组和证据版本</summary><pre>{JSON.stringify({source:edge.source,source_ref:edge.source_ref,locator:edge.locator,context:edge.context,review:edge.review},null,2)}</pre></details>
      </article>)}</> : !error && <p className="evidence-note">正在加载已发布血缘…</p>}
  </section>;
}

function Candidate({ item, isAdmin, busy, act, showSource }) {
  const [reason,setReason]=useState(''),[cardinality,setCardinality]=useState('unknown'),[join,setJoin]=useState('unspecified');
  return <article className="evidence-candidate" data-testid="lineage-candidate">
    <div className="evidence-card-top"><div className="evidence-badges"><span>{lineageLabel(item.kind)}</span><span>{lineageLabel(item.observation)}</span></div><span className={`evidence-status ${item.effective_status}`}>{lineageLabel(item.effective_status)}</span></div>
    <div className="evidence-flow"><Endpoint asset={item.from_asset} column={item.from_column}/><span aria-hidden="true">→</span><Endpoint asset={item.to_asset} column={item.to_column}/></div>
    <code>{item.expression}</code><p className="evidence-note">{lineageLabel(item.technical_status)} · 候选 v{item.version} · {item.source?.title}</p>
    <details><summary>技术检查与证据定位</summary><pre>{JSON.stringify({source_ref:item.source_ref,source:item.source,locator:item.locator,checks:item.checks,context:item.context,review:item.review},null,2)}</pre></details>
    {isAdmin && <><div className="evidence-actions"><button disabled={busy} onClick={()=>showSource(item.source_ref)}>查看原始证据</button>
      {item.status!=='published' && <><button disabled={busy} onClick={()=>act(item,'validate',{expected_version:item.version})}>验证 Schema</button>{item.kind==='join' && <button disabled={busy} onClick={()=>act(item,'validate?profile=true',{expected_version:item.version})}>只读采样（各 ≤1000 行）</button>}</>}
      {item.status==='approved' && <button className="primary-button" disabled={busy || item.effective_status==='stale'} onClick={()=>act(item,'publish',{expected_version:item.version})}>发布候选</button>}
      {['published','approved'].includes(item.status) && <button disabled={busy} onClick={()=>act(item,'withdraw',{expected_version:item.version})}>撤回候选</button>}
    </div>
    {item.status!=='published' && <div className="evidence-review"><label>审核说明<input aria-label="审核说明" value={reason} onChange={e=>setReason(e.target.value)} placeholder="确认业务语义、适用范围或拒绝原因（至少 3 字）"/></label>
      {item.kind==='join' && <div className="evidence-toolbar"><label>业务基数<select aria-label="业务基数" value={cardinality} onChange={e=>setCardinality(e.target.value)}>{['unknown','one_to_one','many_to_one','one_to_many','many_to_many'].map(s=><option key={s} value={s}>{lineageLabel(s)}</option>)}</select></label><label>推荐连接<select aria-label="推荐连接" value={join} onChange={e=>setJoin(e.target.value)}>{['unspecified','INNER JOIN','LEFT JOIN','RIGHT JOIN','FULL JOIN'].map(s=><option key={s} value={s}>{lineageLabel(s)}</option>)}</select></label></div>}
      <div className="evidence-actions"><button disabled={busy || reason.trim().length<3 || item.effective_status==='stale'} onClick={()=>act(item,'review',{expected_version:item.version,decision:'approve',reason,cardinality,recommended_join:join})}>批准候选</button><button disabled={busy || reason.trim().length<3} onClick={()=>act(item,'review',{expected_version:item.version,decision:'reject',reason})}>拒绝候选</button></div></div>}</>}
  </article>;
}

export default function LineageEvidence({ assets=[], isAdmin=false }) {
  const [sources,setSources]=useState([]),[candidates,setCandidates]=useState([]),[busy,setBusy]=useState(false),[error,setError]=useState(''),[message,setMessage]=useState('');
  const [form,setForm]=useState({source_kind:'sql_import',title:'',content:'',source_id:'',source_uri:'',source_version:'',asset_ids:[]});
  const [file,setFile]=useState(null),[status,setStatus]=useState(''),[sourceFilter,setSourceFilter]=useState(''),[source,setSource]=useState(null),[selected,setSelected]=useState(''),[selectedAsset,setSelectedAsset]=useState(null),[invalidReason,setInvalidReason]=useState('');
  const [revision,setRevision]=useState(0);
  const [indexState,setIndexState]=useState(null),[kind,setKind]=useState(''),[visibleLimit,setVisibleLimit]=useState(50);
  const sourceIds = useMemo(()=>[...new Set(assets.map(a=>a.source_id))],[assets]);
  const reload = async()=>{ const [cs,ss,ix] = await Promise.all([request(base+'/candidates'),isAdmin ? request(base+'/sources') : Promise.resolve([]),isAdmin?request(base+'/index/jobs'):Promise.resolve(null)]); setCandidates(cs); setSources(ss);setIndexState(ix);setRevision(n=>n+1); };
  useEffect(()=>{reload().catch(e=>setError(e.message));},[isAdmin]);
  useEffect(()=>{ let live=true; setSelectedAsset(null); if (selected) request('/api/catalog/assets/'+enc(selected)).then(x=>live&&setSelectedAsset(x)).catch(e=>live&&setError(e.message)); return ()=>{live=false;}; },[selected]);
  const run = async(work)=>{setBusy(true);setError('');setMessage('');try {const r=await work();await reload();setMessage(r?.message || '操作已完成；请核对状态。');return r;}catch(e){setError(e.message);}finally{setBusy(false);}};
  const showSource = async id=>{try{setSource(await request(base+'/sources/'+enc(id)));setInvalidReason('');}catch(e){setError(e.message);}};
  const submit = e=>{e.preventDefault();run(async()=>{
    let result;
    if(file && form.source_kind==='business_document'){ const data=new FormData();data.append('file',file);data.append('source_id',form.source_id);data.append('asset_ids',JSON.stringify(form.asset_ids));result=await request(base+'/upload',data); }
    else result=await request(base+'/import',{...form,source_id:form.source_id || null});
    setSourceFilter(result.id);setSource(await request(base+'/sources/'+enc(result.id)));return {message:`证据已保存 · ${lineageLabel(result.state.analysis.status)} · ${result.state.analysis.candidate_count||0} 个候选；未执行导入 SQL。`};
  });};
  const visible = candidates.filter(c=>(!status || c.effective_status===status)&&(!kind||c.kind===kind)&&(!sourceFilter || c.source_ref===sourceFilter));
  const indexHealth = indexState?.health || {pending:indexState?.total_pending || 0,running:0,failed:0};
  useEffect(()=>setVisibleLimit(50),[status,kind,sourceFilter]);
  return <section className="lineage-evidence-panel">
    <div className="evidence-heading"><div><span className="asset-eyebrow">EVIDENCE & LINEAGE</span><h2>关联与字段血缘</h2><p>证据可追溯，语义需审核。技术校验、成功运行与人工批准各自独立。</p></div><button disabled={busy} onClick={()=>run(reload)}>刷新证据</button></div>
    {error && <p className="catalog-error" role="alert">{error}</p>}{message && <p className="evidence-success" role="status">{message}</p>}
    {isAdmin&&(indexHealth.pending+indexHealth.running+indexHealth.failed>0)&&<aside className="evidence-note" role="status">
      {indexHealth.pending} 个等待重试，{indexHealth.running} 个更新中，{indexHealth.failed} 个达到重试上限。
      {indexHealth.worker_enabled===false?'当前未启用向量后台消费，任务已持久保存，启用后继续处理。':'后台自动重试临时故障；达到上限后需管理员重试。'}
      结构化与词法检索已按当前审核状态生效，旧向量不能绕过撤回或权限。
      <details><summary>查看最近索引任务</summary><ul>{(indexState.all_jobs||indexState.jobs||[]).map(job=><li key={job.id}>
        {job.kind || 'lineage'} · {job.status==='completed'?'已完成':job.status==='failed'?'重试上限':job.status==='running'?'更新中':'等待重试'} · 尝试 {job.attempts||0} 次
        {job.next_attempt_at>0&&job.status==='pending'?` · 下次 ${new Date(job.next_attempt_at*1000).toLocaleString()}`:''}
        {job.error?' · 向量索引服务暂不可用':''}
        {job.status==='failed'&&<button type="button" disabled={busy} onClick={()=>run(()=>request(`${base}/index/retry/${enc(job.kind||'lineage')}/${enc(job.id)}`,{}))}>重试该索引任务</button>}
      </li>)}</ul></details>
    </aside>}
    {isAdmin && <details className="evidence-import"><summary>导入 SQL / ETL 定义 / 业务文档</summary><form onSubmit={submit}>
      <div className="evidence-form-grid"><label>证据类型<select aria-label="证据类型" value={form.source_kind} onChange={e=>setForm({...form,source_kind:e.target.value})}>{['sql_import','etl_definition','business_document'].map(s=><option key={s} value={s}>{lineageLabel(s)}</option>)}</select></label><label>证据标题<input required aria-label="证据标题" maxLength={200} value={form.title} onChange={e=>setForm({...form,title:e.target.value})}/></label>
      <label>适用数据源<select aria-label="证据数据源" value={form.source_id} onChange={e=>setForm({...form,source_id:e.target.value,asset_ids:[]})}><option value="">所有已授权数据源（同名可能歧义）</option>{sourceIds.map(s=><option key={s}>{s}</option>)}</select></label><label>来源版本<input value={form.source_version} onChange={e=>setForm({...form,source_version:e.target.value})} placeholder="不填使用内容 SHA-256"/></label><label>来源 URI<input value={form.source_uri} onChange={e=>setForm({...form,source_uri:e.target.value})} placeholder="仅保存引用，不自动访问"/></label></div>
      {form.source_kind==='business_document' && <><label>绑定实际资产（最多 12 个）<select multiple aria-label="文档绑定资产" value={form.asset_ids} onChange={e=>setForm({...form,asset_ids:Array.from(e.target.selectedOptions,o=>o.value).slice(0,12)})}>{assets.filter(a=>!form.source_id||a.source_id===form.source_id).map(a=><option value={a.id} key={a.id}>{a.namespace ? a.namespace+'.' : ''}{a.physical_name} · {a.source_id}</option>)}</select></label><label>原始文件（可选，20 MiB 内）<input type="file" onChange={e=>setFile(e.target.files[0]||null)}/></label></>}
      <label>{form.source_kind==='business_document'?'业务材料原文':'静态 SQL（仅解析，绝不执行）'}<textarea aria-label="证据正文" required={!(file&&form.source_kind==='business_document')} rows={7} maxLength={100000} value={form.content} onChange={e=>setForm({...form,content:e.target.value})}/></label>
      <div className="evidence-actions"><button className="primary-button" disabled={busy}>保存并解析证据</button><button type="button" disabled={busy} onClick={()=>run(()=>request(base+'/collect',{}))}>采集平台已有证据</button><button type="button" disabled={busy} onClick={()=>run(()=>request(base+'/index/retry',{}))}>重试待更新索引</button></div><p className="evidence-note">仅采集平台已记录的成功查询、生产记录和技术快照，不会自动访问外部查询日志。文档提取需调用当前配置模型，生成结果仍为待审核候选。</p>
    </form></details>}
    {isAdmin && <details className="evidence-import"><summary>外部 OpenLineage 风格事件导入</summary><p className="evidence-note">JSON 包含 event 与 bindings；bindings 的键为 namespace/name，值为实际资产 ID。外部上报不等于内部验证。</p><form onSubmit={e=>{e.preventDefault();const text=e.currentTarget.elements.payload.value;run(()=>request(base+'/events',JSON.parse(text)));}}><label>事件与资产绑定 JSON<textarea name="payload" required rows={6}/></label><button disabled={busy}>导入外部事件</button></form></details>}
    {isAdmin && <div className="evidence-toolbar"><label>证据来源<select aria-label="筛选证据来源" value={sourceFilter} onChange={e=>{setSourceFilter(e.target.value);if(e.target.value)showSource(e.target.value);}}><option value="">全部来源</option>{sources.map(s=><option value={s.id} key={s.id}>{s.title} · {lineageLabel(s.state.analysis.status)}</option>)}</select></label><label>审核状态<select aria-label="筛选审核状态" value={status} onChange={e=>setStatus(e.target.value)}><option value="">全部状态</option>{['draft','approved','published','rejected','withdrawn','stale'].map(s=><option key={s} value={s}>{lineageLabel(s)}</option>)}</select></label><label>候选类型<select aria-label="筛选候选类型" value={kind} onChange={e=>setKind(e.target.value)}><option value="">全部类型</option>{['join','table_transform','column_value','column_influence'].map(s=><option key={s} value={s}>{lineageLabel(s)}</option>)}</select></label><span>{visible.length} 个候选</span></div>}
    {source && isAdmin && <aside className="evidence-source" aria-label="原始证据详情"><div className="evidence-heading"><h3>{source.title}</h3><button onClick={()=>setSource(null)}>收起原文</button></div><p>{lineageLabel(source.source_kind)} · {lineageLabel(source.state.status)} · {lineageLabel(source.state.analysis.status)}</p><p className="evidence-note">版本 {source.source_version}<br/>SHA-256 {source.content_hash}<br/>{source.source_uri} · {source.created_at}</p><pre>{source.content}</pre><details><summary>解析结果、参数和块位置</summary><pre>{JSON.stringify({analysis:source.state.analysis,metadata:source.metadata},null,2)}</pre></details><div className="evidence-actions">{source.metadata.storage_path&&<a href={`${base}/sources/${enc(source.id)}/file`}>下载原始文件</a>}<button disabled={busy||source.state.status!=='active'} onClick={()=>run(async()=>{const r=await request(`${base}/sources/${enc(source.id)}/${source.source_kind==='business_document'?'extract':'reanalyze'}`,{});await showSource(source.id);return {message:`${lineageLabel(r.status)} · ${r.candidate_count||0} 个候选。`};})}>{source.source_kind==='business_document'?'调用模型提取候选':'重新解析（不覆盖审核内容）'}</button></div><div className="evidence-toolbar"><label>失效 / 恢复说明<input aria-label="证据状态说明" value={invalidReason} onChange={e=>setInvalidReason(e.target.value)}/></label><button disabled={busy||invalidReason.trim().length<3} onClick={()=>run(async()=>{const r=await request(`${base}/sources/${enc(source.id)}/state`,{expected_version:source.state.version,status:source.state.status==='active'?'invalidated':'active',reason:invalidReason},'PUT');await showSource(source.id);return r;})}>{source.state.status==='active'?'标记证据失效':'恢复证据（需重新审核）'}</button></div></aside>}
    <div className="evidence-candidates">{visible.slice(0,visibleLimit).map(item=><Candidate key={item.id} item={item} isAdmin={isAdmin} busy={busy} showSource={showSource} act={(item,action,payload)=>run(()=>request(`${base}/candidates/${enc(item.id)}/${action}`,payload))}/>)}</div>
    {visible.length>visibleLimit&&<button onClick={()=>setVisibleLimit(n=>n+50)}>再显示 50 个候选（已显示 {visibleLimit}/{visible.length}）</button>}
    {!visible.length&&<p className="evidence-empty">暂无符合条件的候选。导入只产生候选，批准后仍需显式发布。</p>}
    <div className="evidence-browse"><label>查看资产字段血缘<select aria-label="选择血缘资产" value={selected} onChange={e=>setSelected(e.target.value)}><option value="">请选择已发布资产</option>{assets.filter(a=>a.publish_status==='published').map(a=><option key={a.id} value={a.id}>{a.namespace ? a.namespace+'.' : ''}{a.physical_name} · {a.source_id}</option>)}</select></label>{selectedAsset&&<FieldLineagePanel asset={selectedAsset} refreshKey={revision}/>}</div>
  </section>;
}
