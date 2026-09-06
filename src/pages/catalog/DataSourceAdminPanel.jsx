import { useEffect, useState } from 'react';

const SOURCE_KINDS = [
  ['local-sqlite', '本地 SQLite（演示）'],
  ['aws-glue', 'AWS Glue Data Catalog'],
  ['trino', 'Trino / Hive Metastore'],
  ['postgresql', 'PostgreSQL / MySQL'],
];

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || '操作失败');
  return payload;
}

const emptySource = () => ({
  name: '', kind: 'aws-glue', endpoint: '', catalog_name: '',
  auth_type: 'read_only_account', credential: '', enabled: true,
});

/** Admin-only data-source controls. The API enforces this permission as well. */
export default function DataSourceAdminPanel() {
  const [sources, setSources] = useState([]);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');

  const loadSources = async () => {
    try {
      const items = await requestJson('/api/data-sources');
      setSources(items);
      setDraft(current => current || (items[0] ? { ...items[0], credential: '' } : null));
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
      setNotice(action === 'sync' ? `已发现 ${result.table_count} 张表，等待在目录中补充语义并发布。` : result.message || '连接测试成功。');
      await loadSources();
    } catch (error) {
      setNotice(`${action === 'sync' ? '采集' : '测试'}失败：${error.message}`);
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
        <h2>{draft.id ? '编辑数据源' : '添加数据源'}</h2>
        <label>名称<input required value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} placeholder="例如：生产数仓" /></label>
        <label>连接类型<select value={draft.kind} onChange={event => setDraft({ ...draft, kind: event.target.value })}>{SOURCE_KINDS.map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label>
        <label>Endpoint / 地址<input value={draft.endpoint || ''} onChange={event => setDraft({ ...draft, endpoint: event.target.value })} placeholder="例如：https://trino.example.com" /></label>
        <label>Catalog / 数据库<input value={draft.catalog_name || ''} onChange={event => setDraft({ ...draft, catalog_name: event.target.value })} placeholder="例如：warehouse" /></label>
        <label>认证方式<select value={draft.auth_type} onChange={event => setDraft({ ...draft, auth_type: event.target.value })}><option value="none">无需认证</option><option value="read_only_account">只读账号 / Access Key</option><option value="token">访问令牌</option></select></label>
        <label>凭据<input type="password" value={draft.credential || ''} onChange={event => setDraft({ ...draft, credential: event.target.value })} placeholder={draft.credential_configured ? '留空保持已有凭据' : '仅保存加密后的凭据'} /></label>
        <label className="tool-check"><input type="checkbox" checked={Boolean(draft.enabled)} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />启用数据源</label>
        <div className="source-actions"><button disabled={busy}>保存</button>{draft.id && <><button type="button" className="outline-button" disabled={busy || !draft.enabled} onClick={() => runAction('test')}>测试连接</button><button type="button" className="outline-button" disabled={busy || !draft.enabled} onClick={() => runAction('sync')}>采集表结构</button></>}</div>
      </> : <p>点击“添加数据源”开始配置。</p>}</form>
      <p className="source-note">请配置只读账号。采集到的是技术元数据草稿；补充业务语义、权限和质量信息并发布后，资产才会进入数据目录和 RAG。</p>
    </div>
    {notice && <p className="save-notice catalog-notice" role="status">{notice}</p>}
  </section>;
}
