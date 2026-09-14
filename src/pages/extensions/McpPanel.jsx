import React, { useEffect, useMemo, useRef, useState } from 'react';
import './ExtensionManagement.css';

const emptyServer = {
  id: '', name: '', description: '', transport: 'streamable-http', endpoint: '',
  auth_type: 'bearer', api_key: '', timeout_seconds: 30, enabled: true,
};

async function jsonRequest(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || '请求失败');
  return payload;
}

export default function McpPanel({ canManage = false }) {
  const [servers, setServers] = useState([]);
  const [serverId, setServerId] = useState('');
  const [draft, setDraft] = useState(emptyServer);
  const [tools, setTools] = useState([]);
  const [query, setQuery] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const toolRequest = useRef(0);

  const loadServers = async preferred => {
    const values = await jsonRequest('/api/mcp-servers');
    if (!Array.isArray(values)) throw new Error('MCP Server 接口返回格式不正确');
    setServers(values);
    setServerId(current => {
      const candidate = preferred || current;
      return values.some(item => item.id === candidate) ? candidate : values[0]?.id || '';
    });
  };
  const loadTools = async id => {
    const request = ++toolRequest.current;
    const values = id ? await jsonRequest(`/api/mcp-servers/${id}/tools`) : [];
    if (request === toolRequest.current) setTools(Array.isArray(values) ? values : []);
  };

  useEffect(() => { loadServers().catch(error => setNotice(error.message)); }, []);
  useEffect(() => {
    const selected = servers.find(item => item.id === serverId);
    if (selected) {
      setDraft({ ...selected, api_key: '' });
      loadTools(serverId).catch(error => setNotice(error.message));
    } else if (!serverId) {
      setDraft(emptyServer); setTools([]);
    }
  }, [serverId, servers]);

  const filtered = useMemo(() => {
    const key = query.trim().toLowerCase();
    return key ? tools.filter(tool => `${tool.display_name} ${tool.name} ${tool.description}`.toLowerCase().includes(key)) : tools;
  }, [query, tools]);
  const savedDraft = servers.find(item => item.id === draft.id);
  const hasUnsavedChanges = Boolean(savedDraft && JSON.stringify({ ...savedDraft, api_key: '' }) !== JSON.stringify({ ...draft, api_key: '' }));

  const saveServer = async event => {
    event.preventDefault(); setBusy(true); setNotice('');
    if (!canManage) { setBusy(false); setNotice('仅平台管理员可以修改 MCP 配置。'); return; }
    try {
      const creating = !draft.id;
      const method = draft.id ? 'PUT' : 'POST';
      const url = draft.id ? `/api/mcp-servers/${draft.id}` : '/api/mcp-servers';
      const saved = await jsonRequest(url, {
        method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft),
      });
      let message = 'MCP Server 配置已保存。';
      if (creating && saved.enabled) {
        try {
          const synced = await jsonRequest(`/api/mcp-servers/${saved.id}/sync`, { method: 'POST' });
          message = `MCP Server 已保存并自动发现 ${synced.tool_count} 个工具。`;
        } catch (syncError) {
          message = `Server 已保存，但自动发现失败：${syncError.message}`;
        }
      }
      setNotice(message); await loadServers(saved.id); await loadTools(saved.id);
    } catch (error) { setNotice(`保存失败：${error.message}`); } finally { setBusy(false); }
  };

  const serverAction = async action => {
    if (!draft.id || !canManage) return;
    setBusy(true); setNotice('');
    try {
      const result = await jsonRequest(`/api/mcp-servers/${draft.id}/${action}`, { method: 'POST' });
      setNotice(action === 'sync' ? `同步完成，发现 ${result.tool_count} 个工具。` : `连接成功，发现 ${result.tool_count} 个工具。`);
      await loadServers(draft.id); await loadTools(draft.id);
    } catch (error) { setNotice(`${action === 'sync' ? '同步' : '测试'}失败：${error.message}`); } finally { setBusy(false); }
  };

  const updateTool = async (tool, changes) => {
    if (!canManage || tool.unavailable) return;
    try {
      const saved = await jsonRequest(`/api/mcp-tools/${encodeURIComponent(tool.id)}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: tool.enabled, risk_level: tool.risk_level,
          required_permission: tool.required_permission, ...changes,
        }),
      });
      setTools(values => values.map(item => item.id === saved.id ? saved : item));
      setNotice(`工具「${saved.display_name}」已更新。`);
    } catch (error) { setNotice(`工具更新失败：${error.message}`); }
  };

  const removeServer = async () => {
    if (!draft.id || !canManage || busy) return;
    if (!window.confirm(`确定永久删除 MCP Server“${draft.name}”吗？已发现工具和所有 Agent 绑定也会一并删除。`)) return;
    setBusy(true); setNotice('');
    try {
      const deleted = await jsonRequest(`/api/mcp-servers/${draft.id}`, { method: 'DELETE' });
      setServerId(''); setTools([]);
      await loadServers();
      setNotice(`MCP Server“${deleted.name || draft.name}”已删除，并解除 ${deleted.updated_agent_count || 0} 个 Agent 绑定。`);
    } catch (error) { setNotice(`删除失败：${error.message}`); } finally { setBusy(false); }
  };

  return <div className="extension-page extension-management">
    <div className="extension-title-row"><div><h2>MCP</h2><p className="page-subtitle">管理 MCP Server，并自动发现、配置和启用其工具。</p></div>{canManage && <button className="outline-button" onClick={() => { setServerId(''); setDraft(emptyServer); setTools([]); }}>＋ 添加 Server</button>}</div>
    {!canManage && <p className="page-subtitle">当前账号仅可查看 MCP 配置；请联系平台管理员申请管理权限。</p>}
    {notice && <p className="save-notice catalog-notice" role="status">{notice}</p>}
    <div className="extension-console">
      <aside className="extension-nav">
        {servers.map(server => <button key={server.id} className={server.id === serverId ? 'active' : ''} onClick={() => setServerId(server.id)}><b>{server.name}</b><span>{server.enabled ? '已启用' : '已停用'} · {server.tool_count} 个工具</span><small>{server.last_test_status || '尚未连接'}</small></button>)}
        {!servers.length && <p>还没有 MCP Server。</p>}
      </aside>
      <main className="extension-detail">
        <form className="extension-editor" onSubmit={saveServer}>
          <div className="editor-heading"><div><h3>{draft.id ? 'Server 配置' : '添加 MCP Server'}</h3><p>凭据只会加密保存，页面不会回显原值。</p></div><label className="switch-label"><input type="checkbox" disabled={!canManage || busy} checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />启用</label></div>
          <fieldset disabled={!canManage || busy} className="extension-form-grid">
            <label>名称<input required value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} placeholder="例如：内部指标服务" /></label>
            <label>Transport<select value={draft.transport} onChange={event => setDraft({ ...draft, transport: event.target.value })}><option value="streamable-http">Streamable HTTP</option></select></label>
            <label className="wide">说明<input value={draft.description || ''} onChange={event => setDraft({ ...draft, description: event.target.value })} placeholder="这个 Server 提供哪些能力" /></label>
            <label className="wide">Endpoint<input required value={draft.endpoint} onChange={event => setDraft({ ...draft, endpoint: event.target.value })} placeholder="https://mcp.example.com/mcp" /></label>
            <label>认证方式<select value={draft.auth_type} onChange={event => setDraft({ ...draft, auth_type: event.target.value })}><option value="none">无需认证</option><option value="bearer">Bearer Token</option></select></label>
            <label>超时（秒）<input type="number" min="3" max="300" value={draft.timeout_seconds} onChange={event => setDraft({ ...draft, timeout_seconds: Number(event.target.value) })} /></label>
            <label className="wide">访问凭据<input type="password" value={draft.api_key || ''} onChange={event => setDraft({ ...draft, api_key: event.target.value })} placeholder={draft.credential_configured ? '留空保留已有凭据' : 'Bearer Token（可选）'} /></label>
          </fieldset>
          {hasUnsavedChanges && <p className="page-subtitle">配置已修改，请先保存后再测试或刷新工具。</p>}<div className="extension-actions">{canManage && <button disabled={busy}>保存配置</button>}{draft.id && canManage && <><button type="button" className="outline-button" disabled={busy || hasUnsavedChanges} onClick={() => serverAction('test')}>测试连接</button><button type="button" className="outline-button" disabled={busy || hasUnsavedChanges || !draft.enabled} onClick={() => serverAction('sync')}>刷新工具</button><button type="button" className="danger-button" disabled={busy} onClick={removeServer}>删除 Server</button></>}</div>
        </form>
        {draft.id && <section className="managed-items">
          <div className="managed-items-heading"><div><h3>已发现工具</h3><p>{tools.length} 个工具；发现后默认启用，仅对绑定该 Server 或工具的 Agent 可用。</p></div><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索工具" /></div>
          <div className="managed-tool-list">{filtered.map(tool => <article key={tool.id}>
            <div className="managed-tool-title"><div><b>{tool.display_name}</b><code>{tool.name}</code>{tool.unavailable && <small>· Server 当前不可用</small>}</div>{canManage && <label className="switch-label"><input type="checkbox" disabled={busy || tool.unavailable} checked={tool.enabled} onChange={event => updateTool(tool, { enabled: event.target.checked })} />{tool.enabled ? '启用' : '停用'}</label>}</div>
            <p>{tool.description || '无说明'}</p>
            <div className="managed-tool-controls"><label>风险<select disabled={!canManage || busy || tool.unavailable} value={tool.risk_level} onChange={event => updateTool(tool, { risk_level: event.target.value })}><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label><label>所需权限<input disabled={!canManage || busy || tool.unavailable} value={tool.required_permission || ''} onChange={event => setTools(values => values.map(item => item.id === tool.id ? { ...item, required_permission: event.target.value } : item))} onBlur={event => updateTool(tool, { required_permission: event.target.value })} placeholder="可留空" /></label></div>
            <details><summary>查看 Schema 与 annotations</summary><pre>{JSON.stringify({ inputSchema: tool.input_schema, outputSchema: tool.output_schema, annotations: tool.annotations }, null, 2)}</pre></details>
          </article>)}{!filtered.length && <p>没有匹配的工具。点击“刷新工具”从 Server 同步。</p>}</div>
        </section>}
      </main>
    </div>
  </div>;
}
