import { useEffect, useMemo, useState } from 'react';

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || '加载失败');
  return payload;
}

function ChoiceList({ title, items, selected, onChange, disabled }) {
  return <fieldset className="access-choice-list"><legend>{title}</legend>{items.map(item => <label key={item.id}>
    <input type="checkbox" checked={selected.includes(item.id)} disabled={disabled?.(item)} onChange={() => onChange(item.id)} />
    <span><b>{item.display_name}</b><small>{item.description}</small></span>
  </label>)}</fieldset>;
}

function UserManagement({ data, currentUser, onSaved }) {
  const [selectedId, setSelectedId] = useState('');
  const [draft, setDraft] = useState(null);
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const selected = useMemo(() => data.users.find(item => item.id === selectedId) || data.users[0], [data.users, selectedId]);

  useEffect(() => { if (selected) setDraft({ ...selected, role_ids: [...selected.role_ids], group_ids: [...selected.group_ids] }); }, [selected]);
  if (!selected || !draft) return <p className="page-subtitle">暂无用户。</p>;
  const flip = (field, id) => setDraft(current => ({ ...current, [field]: current[field].includes(id) ? current[field].filter(item => item !== id) : [...current[field], id] }));
  const save = async event => {
    event.preventDefault(); setBusy(true); setNotice('');
    try {
      await requestJson(`/api/admin/users/${draft.id}/access`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ role_ids: draft.role_ids, group_ids: draft.group_ids, is_active: draft.is_active }) });
      setNotice('用户角色与数据访问范围已更新。'); await onSaved();
    } catch (error) { setNotice(`保存失败：${error.message}`); } finally { setBusy(false); }
  };
  return <div className="system-management-grid"><aside className="system-user-list">{data.users.map(user => <button type="button" key={user.id} className={selected.id === user.id ? 'active' : ''} onClick={() => setSelectedId(user.id)}><b>{user.username}</b><span>{user.is_active ? '已启用' : '已停用'} · {user.role_ids.map(id => data.roles.find(role => role.id === id)?.display_name).filter(Boolean).join('、')}</span></button>)}</aside>
    <form className="system-access-editor" onSubmit={save}><div className="system-editor-heading"><div><h2>{draft.username}</h2><p>注册于 {draft.created_at || '—'}</p></div><label className="tool-check"><input type="checkbox" checked={Boolean(draft.is_active)} disabled={draft.id === currentUser.id} onChange={event => setDraft({ ...draft, is_active: event.target.checked })} />启用账号</label></div>
      <ChoiceList title="平台角色" items={data.roles} selected={draft.role_ids} onChange={id => flip('role_ids', id)} disabled={role => draft.id === currentUser.id && role.id === 'role-platform-admin'} />
      <ChoiceList title="用户组与业务域" items={data.groups.map(group => ({ ...group, description: `${group.description} 可访问：${group.domain_ids.map(id => data.domains.find(domain => domain.id === id)?.display_name || id).join('、') || '未配置业务域'}` }))} selected={draft.group_ids} onChange={id => flip('group_ids', id)} />
      <button className="save-agent" disabled={busy}>保存访问配置</button>{notice && <p className="save-notice" role="status">{notice}</p>}
    </form></div>;
}

function RolePolicy({ data }) {
  return <div className="system-card-grid">{data.roles.map(role => <article key={role.id}><h3>{role.display_name}</h3><p>{role.description}</p><div className="policy-tags">{role.policies.length ? role.policies.map((policy, index) => <code key={index}>{policy.effect} · {policy.action} · {policy.resource_type}/{policy.resource_id}</code>) : <span>无默认策略；由用户组与业务域授权。</span>}</div></article>)}</div>;
}

function GroupDomain({ data }) {
  return <div className="system-card-grid">{data.groups.map(group => <article key={group.id}><h3>{group.display_name}</h3><p>{group.description}</p><b>{group.member_count} 位成员</b><div className="policy-tags">{group.domain_ids.length ? group.domain_ids.map(id => <code key={id}>{data.domains.find(domain => domain.id === id)?.display_name || id}</code>) : <span>尚未绑定业务域</span>}</div></article>)}</div>;
}

function AuditLog({ rows, loading, error }) {
  if (loading) return <p className="page-subtitle">正在加载审计日志…</p>;
  if (error) return <p className="catalog-error">{error}</p>;
  return <div className="audit-log-list">{rows.length ? rows.map(row => <article key={row.id}><div><b>{row.action}</b><span>{row.entity_type} · {row.entity_id}</span></div><p>操作人：{row.actor_username || '系统'} · {row.created_at}</p>{Object.keys(row.detail || {}).length > 0 && <details><summary>查看变更</summary><pre>{JSON.stringify(row.detail, null, 2)}</pre></details>}</article>) : <p className="page-subtitle">暂无审计记录。</p>}</div>;
}

export default function SystemManagementPage({ currentUser }) {
  const [tab, setTab] = useState('users'); const [data, setData] = useState(null); const [error, setError] = useState(''); const [audit, setAudit] = useState([]); const [auditLoading, setAuditLoading] = useState(false); const [auditError, setAuditError] = useState('');
  const load = async () => { setError(''); try { setData(await requestJson('/api/admin/overview')); } catch (loadError) { setError(loadError.message); } };
  const loadAudit = async () => { setAuditLoading(true); setAuditError(''); try { setAudit(await requestJson('/api/admin/audit-logs')); } catch (loadError) { setAuditError(loadError.message); } finally { setAuditLoading(false); } };
  useEffect(() => { load(); }, []);
  useEffect(() => { if (tab === 'audit') loadAudit(); }, [tab]);
  const panels = { users: data && <UserManagement data={data} currentUser={currentUser} onSaved={load} />, roles: data && <RolePolicy data={data} />, groups: data && <GroupDomain data={data} />, audit: <AuditLog rows={audit} loading={auditLoading} error={auditError} /> };
  return <section className="page system-management-page"><div className="catalog-header"><div><h1>系统管理</h1><p className="page-subtitle">管理用户的系统角色、用户组与可访问业务域。所有授权变更都会记录审计日志。</p></div><button className="outline-button" onClick={load}>刷新</button></div><div className="catalog-tabs">{[['users','用户管理'],['roles','角色与权限'],['groups','用户组与业务域'],['audit','审计日志']].map(([id,label]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{label}</button>)}</div>{error ? <p className="catalog-error">{error}</p> : data || tab === 'audit' ? panels[tab] : <p className="page-subtitle">正在加载系统管理信息…</p>}</section>;
}
