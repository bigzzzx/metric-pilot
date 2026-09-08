import { useState } from 'react';
import './AssetReviewPanel.css';

const sensitivities = [['public','公开'],['internal','内部'],['sensitive','敏感'],['restricted','受限']];
const aliases = text => text.split(/[,，]/).map(value => value.trim()).filter(Boolean);

async function submit(url, method, data) {
  const response = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, ...(data ? { body: JSON.stringify(data) } : {}) });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : '填写内容不符合要求，请检查字段。');
  return payload;
}

export default function AssetReviewPanel({ asset, domains, onUpdated }) {
  const [draft, setDraft] = useState(() => ({
    version: asset.catalog_version, display_name: asset.display_name, description: asset.description,
    aliases: asset.aliases || [], domain_id: asset.domain_id, warehouse_layer: asset.warehouse_layer,
    data_grain: asset.data_grain || '', sensitivity: asset.sensitivity,
    columns: asset.columns.map(c => ({ id: c.id, display_name: c.display_name, description: c.description,
      aliases: c.aliases || [], semantic_role: c.semantic_role || '', sensitivity: c.sensitivity,
      masking_strategy: ['none','partial','redact'].includes(c.masking_strategy) ? c.masking_strategy : 'redact' })),
  }));
  const [busy, setBusy] = useState(false), [notice, setNotice] = useState(''), [reason, setReason] = useState('');
  const published = asset.publish_status === 'published';
  const field = (name, value) => setDraft(current => ({ ...current, [name]: value }));
  const columnField = (id, name, value) => setDraft(current => ({ ...current, columns: current.columns.map(c => c.id === id ? { ...c, [name]: value } : c) }));
  const act = async action => {
    setBusy(true); setNotice('');
    let mutated = false;
    try {
      let result;
      if (action === 'save' || action === 'publish') {
        result = await submit(`/api/catalog/admin/assets/${asset.id}/review`, 'PUT', draft);
        mutated = true;
        field('version', result.version);
        if (action === 'publish') result = await submit(`/api/catalog/admin/assets/${asset.id}/publish?version=${result.version}`, 'POST');
      } else {
        result = await submit(`/api/catalog/admin/assets/${asset.id}/${action}`, 'POST', { version: draft.version, reason });
        mutated = true;
      }
      await onUpdated(result.message);
    } catch (error) {
      setNotice(error.message);
      if (mutated) await onUpdated(`草稿已保存，但发布未完成：${error.message}`);
    } finally { setBusy(false); }
  };
  return <section className="asset-review">
    <h3>{published ? '发布管理' : '审核元数据'}</h3>
    <p className="page-subtitle">源库已有的注释已自动填入。核对业务域、粒度与敏感字段后发布；源信息完整时无需重复填写。</p>
    {asset.review_note && <p className="sample-notice">最近审核记录：{asset.review_note}</p>}
    {published ? <p>已发布内容如需修改，请先撤回。撤回后普通用户和模型无法检索该资产，修改完成可重新发布。</p> : <fieldset disabled={busy}>
      <div className="review-grid">
        <label>业务名称<input value={draft.display_name} onChange={e => field('display_name', e.target.value)}/></label>
        <label>业务域<select value={draft.domain_id} onChange={e => field('domain_id', e.target.value)}>{domains.map(d => <option key={d.id} value={d.id}>{d.display_name}</option>)}</select></label>
        <label>数仓分层<select value={draft.warehouse_layer} onChange={e => field('warehouse_layer', e.target.value)}>{['ODS','DWD','DIM','DWS','ADS','UNKNOWN'].map(v => <option key={v}>{v}</option>)}</select></label>
        <label>敏感级别<select value={draft.sensitivity} onChange={e => field('sensitivity', e.target.value)}>{sensitivities.map(([v,t]) => <option key={v} value={v}>{t}</option>)}</select></label>
        <label>业务别名（逗号分隔）<input defaultValue={draft.aliases.join(',')} onBlur={e => field('aliases', aliases(e.target.value))}/></label>
        <label>数据粒度<input placeholder="例如：一行代表一笔订单" value={draft.data_grain} onChange={e => field('data_grain', e.target.value)}/></label>
        <label className="review-wide">表说明<textarea rows={3} value={draft.description} onChange={e => field('description', e.target.value)}/></label>
      </div>
      <h4>字段说明与脱敏</h4>
      <div className="review-columns">{draft.columns.map((c,i) => <article key={c.id}>
        <b>{asset.columns[i].physical_name} <small>{asset.columns[i].data_type}</small></b>
        <div className="review-grid">
          <label>字段名称<input value={c.display_name} onChange={e => columnField(c.id,'display_name',e.target.value)}/></label>
          <label>字段别名<input defaultValue={c.aliases.join(',')} onBlur={e => columnField(c.id,'aliases',aliases(e.target.value))}/></label>
          <label className="review-wide">字段说明<input value={c.description} onChange={e => columnField(c.id,'description',e.target.value)}/></label>
          <label>语义角色<input placeholder="如 identifier / dimension / measure" value={c.semantic_role} onChange={e => columnField(c.id,'semantic_role',e.target.value)}/></label>
          <label>敏感级别<select value={c.sensitivity} onChange={e => columnField(c.id,'sensitivity',e.target.value)}>{sensitivities.map(([v,t]) => <option key={v} value={v}>{t}</option>)}</select></label>
          <label>样例脱敏<select value={c.masking_strategy} onChange={e => columnField(c.id,'masking_strategy',e.target.value)}><option value="none">默认（敏感字段仍会遮蔽）</option><option value="partial">仅保留首字符</option><option value="redact">全部遮蔽</option></select></label>
        </div>
      </article>)}</div>
    </fieldset>}
    <label className="review-reason">{published ? '撤回原因' : '退回原因（退回时必填）'}<input disabled={busy} value={reason} onChange={e => setReason(e.target.value)} placeholder="填写原因，记录在审核日志中"/></label>
    <div className="review-actions">{published ? <button disabled={busy || !reason.trim()} onClick={() => act('withdraw')}>撤回发布</button> : <>
      <button className="outline-button" disabled={busy} onClick={() => act('save')}>保存草稿</button>
      <button disabled={busy} onClick={() => act('publish')}>保存并发布</button>
      <button className="outline-button" disabled={busy || !reason.trim()} onClick={() => act('reject')}>退回</button>
    </>}{busy && <span role="status">正在保存或同步索引…</span>}</div>
    {notice && <p role="alert" className="catalog-error">{notice}</p>}
  </section>;
}
