import { useEffect, useState } from 'react';
import './DataSourceAdminPanel.css';

const SOURCE_KINDS = [
  ['local-sqlite', '本地 SQLite（演示）'],
  ['postgresql', 'PostgreSQL'],
  ['aws-glue', 'AWS Glue Data Catalog'],
  ['trino', 'Trino / Hive Metastore'],
];

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || '操作失败');
  return payload;
}

const emptySource = () => ({
  name: '', kind: 'postgresql', endpoint: '127.0.0.1:15433', catalog_name: '',
  auth_type: 'read_only_account', username: '', credential: '', enabled: true,
});

/** Admin-only data-source controls. The API enforces this permission as well. */
export default function DataSourceAdminPanel({ onCatalogChanged = () => {} }) {
  const [sources, setSources] = useState([]);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');

  const loadSources = async () => {
    try {
      const items = await requestJson('/api/data-sources');
      setSources(items);
      setDraft(current => {
        const selected = current?.id ? items.find(item => item.id === current.id) : null;
        return selected ? { ...selected, credential: '' } : (items[0] ? { ...items[0], credential: '' } : null);
      });
    } catch (error) {
      setNotice(`无法读取数据源：${error.message}`);
    }
  };

  useEffect(() => { loadSources(); }, []);

  const save = async event => {
    event.preventDefault();
    if (!draft) return;
    setBusy(true); setNotice('');
    try {
      const url = draft.id ? `/api/data-sources/${draft.id}` : '/api/data-sources';
      const method = draft.id ? 'PUT' : 'POST';
      const saved = await requestJson(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft) });
      setDraft({ ...saved, credential: '' });
      setNotice('数据源配置已保存。');
      await loadSources();
    } catch (error) {
      setNotice(`保存失败：${error.message}`);
    } finally { setBusy(false); }
  };

  const runAction = async action => {
    if (!draft?.id) return;
    setBusy(true); setNotice('');
    try {
      const result = await requestJson(`/api/data-sources/${draft.id}/${action}`, { method: 'POST' });
      setNotice(action === 'sync' ? `已发现 ${result.table_count} 张表、${result.relationship_count || 0} 条关系，并保存为目录草稿。` : result.message || '连接测试成功。');
      await loadSources();
      if (action === 'sync') onCatalogChanged();
    } catch (error) {
      setNotice(`${action === 'sync' ? '采集' : '测试'}失败：${error.message}`);
    } finally { setBusy(false); }
  };

  const remove = async () => {
    if (!draft?.id || draft.id === 'local-ecommerce-demo') return;
    if (!window.confirm(`确定删除数据源“${draft.name}”吗？\n\n尚未发布的目录草稿会一并删除；已经发布资产的数据源不能直接删除。`)) return;
    setBusy(true); setNotice('');
    try {
      const result = await requestJson(`/api/data-sources/${draft.id}`, { method: 'DELETE' });
      const items = await requestJson('/api/data-sources');
      setSources(items);
      setDraft(items[0] ? { ...items[0], credential: '' } : null);
      setNotice(`数据源已删除，同时清理 ${result.draft_assets_deleted || 0} 个未发布资产草稿。`);
      onCatalogChanged();
    } catch (error) {
      setNotice(`删除失败：${error.message}`);
    } finally { setBusy(false); }
  };

  return <section className="source-admin-page">
    <div className="source-admin-heading"><div><h2>数据源管理</h2><p className="page-subtitle">仅平台管理员可管理连接配置、同步技术元数据和启停数据源。凭据不会返回到浏览器。</p></div></div>
    <div className="source-config-panel">
      <aside className="source-list">
        <button type="button" className="add-source" onClick={() => { setDraft(emptySource()); setNotice(''); }}>＋ 添加数据源</button>
        {sources.map(source => <button type="button" key={source.id} className={draft?.id === source.id ? 'active' : ''} onClick={() => { setDraft({ ...source, credential: '' }); setNotice(''); }}><b>{source.name}</b><span>{source.kind} · {source.enabled ? '已启用' : '已停用'}</span></button>)}
      </aside>
      <form className="source-form" onSubmit={save}>{draft ? <>
        <div className="source-form-heading"><h2>{draft.id ? '编辑数据源' : '添加数据源'}</h2><label className="source-enabled-control"><span>{draft.enabled ? '已启用' : '已停用'}</span><input type="checkbox" checked={Boolean(draft.enabled)} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} /><i aria-hidden="true" /></label></div>
        <label>名称<input required value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} placeholder="例如：生产数仓" /><small>仅用于页面识别，不影响数据库中的实际名称。</small></label>
        <label>连接类型<select value={draft.kind} onChange={event => setDraft({ ...draft, kind: event.target.value })}>{SOURCE_KINDS.map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select><small>选择真实数据库或元数据服务的类型。</small></label>
        <label>Endpoint / 地址<input value={draft.endpoint || ''} onChange={event => setDraft({ ...draft, endpoint: event.target.value })} placeholder={draft.kind === 'postgresql' ? '例如：10.0.0.5:5432' : '例如：https://trino.example.com'} /><small>{draft.kind === 'postgresql' ? '本机开发经 SSH 隧道填写 127.0.0.1:15433；部署环境填写内网地址。' : '填写服务端点及端口。'}</small></label>
        <label>Catalog / 数据库<input value={draft.catalog_name || ''} onChange={event => setDraft({ ...draft, catalog_name: event.target.value })} placeholder="例如：data_agent_warehouse" /><small>PostgreSQL 中填写数据库名，不是 Schema 名。</small></label>
        <label>认证方式<select value={draft.auth_type} onChange={event => setDraft({ ...draft, auth_type: event.target.value })}><option value="none">无需认证</option><option value="read_only_account">只读账号 / Access Key</option><option value="token">访问令牌</option></select><small>数据库请使用专门的只读账号。</small></label>
        {draft.kind === 'postgresql' && <label>只读用户名<input value={draft.username || ''} onChange={event => setDraft({ ...draft, username: event.target.value })} placeholder="例如：data_agent_reader" autoComplete="off" /><small>当前测试数仓使用 data_agent_reader。</small></label>}
        <label>{draft.kind === 'postgresql' ? '密码' : '凭据'}<input type="password" value={draft.credential || ''} onChange={event => setDraft({ ...draft, credential: event.target.value })} placeholder={draft.credential_configured ? '留空保持已有密码' : '保存时由服务端加密'} autoComplete="new-password" /><small>编辑时留空不会覆盖原密码；凭据不会返回浏览器。</small></label>
        <div className="source-actions"><button disabled={busy}>保存</button>{draft.id && <><button type="button" className="outline-button" disabled={busy || !draft.enabled} onClick={() => runAction('test')}>测试连接</button><button type="button" className="outline-button" disabled={busy || !draft.enabled} onClick={() => runAction('sync')}>采集表结构</button>{draft.id !== 'local-ecommerce-demo' && <button type="button" className="danger-button" disabled={busy} onClick={remove}>删除数据源</button>}</>}</div>
      </> : <p>点击“添加数据源”开始配置。</p>}</form>
      <p className="source-note">请配置只读账号。数据源中已有的表、字段说明和主外键会被一并采集；目录审核发布后进入 RAG。</p>
    </div>
    {notice && <p className="save-notice catalog-notice" role="status">{notice}</p>}
  </section>;
}
