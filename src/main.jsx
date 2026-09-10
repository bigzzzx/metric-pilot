import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import catalog from '../metadata/catalog.json';
import KnowledgeBasePage from './pages/extensions/KnowledgeBasePage.jsx';
import CatalogPage from './pages/catalog/CatalogPage.jsx';
import SystemManagementPage from './pages/admin/SystemManagementPage.jsx';
import ManagedMcpPanel from './pages/extensions/McpPanel.jsx';
import ManagedSkillsPanel from './pages/extensions/SkillsPanel.jsx';
import ToolApprovalCard, { PermissionDeniedCard } from './components/chat/ToolApprovalCard.jsx';
import MessageText from './components/chat/MessageText.jsx';
import QueryTraceDetails from './components/chat/QueryTraceDetails.jsx';
import './styles.css';

const nav = [
  ['new-chat', '✦', '创建新对话'],
  ['catalog', '▤', '数据目录'],
  ['extensions', '⌘', '智能体扩展'],
  ['manage', '⚙', '智能体管理'],
];

function AssistantAnswer({ data }) {
  const rows = data.result || [];
  const columns = rows[0] ? Object.keys(rows[0]) : [];
  return <article className="message assistant-message">
    <div className="message-name">✦ Data Agent <span>{data.intent}</span></div>
    {data.permission_error && <PermissionDeniedCard error={data.permission_error} />}
    {data.approval && <ToolApprovalCard approval={data.approval} />}
    {!data.approval && (data.answer ? <MessageText>{data.answer}</MessageText> : <p className="stream-status">{data.status || '正在处理中…'}</p>)}
    {data.sql && <details><summary>查看 SQL</summary><pre>{data.sql}</pre></details>}
    {columns.length > 0 && <div className="table-wrap"><table><thead><tr>{columns.map(x => <th key={x}>{x}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{columns.map(x => <td key={x}>{row[x]}</td>)}</tr>)}</tbody></table></div>}
    {data.trace && <QueryTraceDetails trace={data.trace} />}
  </article>;
}

function Composer({ onSend, busy }) {
  const [value, setValue] = useState('');
  const submit = event => { event.preventDefault(); onSend(value); setValue(''); };
  const handleKeyDown = event => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (value.trim() && !busy) { onSend(value); setValue(''); }
    }
  };
  return <form className="home-composer" onSubmit={submit}>
    <textarea value={value} onChange={event => setValue(event.target.value)} onKeyDown={handleKeyDown} placeholder="发消息给 Data Agent…" rows="3" />
    <div className="composer-toolbar">
      <span className="composer-add">＋</span>
      <span className="toolbar-spacer" />
      <button type="submit" disabled={busy || !value.trim()} aria-label="发送">↑</button>
    </div>
  </form>;
}

function ChatHome({ session, messages, setMessages, onFirstMessage, onNewConversation, onConversationChanged }) {
  const [busy, setBusy] = useState(false);
  const send = async text => {
    const message = text.trim(); if (!message || busy) return;
    const clientMessageId = crypto.randomUUID();
    setBusy(true); setMessages(items => [...items, { id: clientMessageId, role: 'user', text: message, clientMessageId }]); onFirstMessage(message);
    let assistantStarted = false;
    const updateAssistant = updater => setMessages(items => {
      const last = items[items.length - 1];
      if (last?.role === 'assistant' && last.streaming) {
        return [...items.slice(0, -1), { ...last, data: updater(last.data) }];
      }
      assistantStarted = true;
      return [...items, { role: 'assistant', streaming: true, data: updater({ intent: 'CHAT', answer: '', status: '正在处理中…' }) }];
    });
    const finishAssistant = data => setMessages(items => {
      const last = items[items.length - 1];
      if (last?.role === 'assistant' && last.streaming) return [...items.slice(0, -1), { role: 'assistant', streaming: false, data }];
      return [...items, { role: 'assistant', streaming: false, data }];
    });
    try {
      const response = await fetch('/api/chat/stream', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message, session_id: session, client_message_id: clientMessageId }) });
      if (!response.ok || !response.body) {
        const raw = await response.text();
        let data; try { data = raw ? JSON.parse(raw) : {}; } catch { data = {}; }
        throw Error(data.detail || `服务端返回异常（HTTP ${response.status}）：${raw || '无响应内容'}`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      const handleEvent = block => {
        const event = (block.match(/^event:\s*(.+)$/m)?.[1] || 'message').trim();
        const raw = block.match(/^data:\s*(.+)$/m)?.[1];
        if (!raw) return;
        let data; try { data = JSON.parse(raw); } catch { return; }
        if (event === 'status') updateAssistant(current => ({ ...current, status: data.label }));
        if (event === 'delta') updateAssistant(current => ({ ...current, answer: (current.answer || '') + data.content, status: '' }));
        if (event === 'approval_required') updateAssistant(current => ({ ...current, approval: data, status: '等待批准本次高风险操作' }));
        if (event === 'permission_denied') updateAssistant(current => ({ ...current, permission_error: data, status: '' }));
        if (event === 'done') finishAssistant(data);
        if (event === 'error') throw Error(`${data.detail || '流式响应失败'}${data.request_id ? `（${data.request_id}）` : ''}`);
      };
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r/g, '');
        const blocks = buffer.split('\n\n'); buffer = blocks.pop();
        blocks.forEach(handleEvent);
        if (done) break;
      }
    } catch (error) {
      const data = { intent: 'ERROR', answer: error.message };
      if (assistantStarted) finishAssistant(data);
      else setMessages(items => [...items, { role: 'assistant', data }]);
    }
    finally { setBusy(false); onConversationChanged?.(); }
  };
  return <section className="chat-page">
    {messages.length === 0 ? <div className="home-welcome"><h1>你好，有什么可以帮助你的？</h1><Composer {...{ onSend: send, busy }} /></div> : <><div className="conversation-list">{messages.map((item, index) => item.role === 'user' ? <div className="user-bubble" key={item.id || item.clientMessageId || index}>{item.text}</div> : <AssistantAnswer key={item.id || index} data={item.data} />)}</div><div className="chat-docked"><Composer {...{ onSend: send, busy }} /></div><button className="new-thread-small" onClick={onNewConversation}>新对话</button></>}
  </section>;
}

function Catalog() {
  const tables = catalog.tables || catalog;
  const [chosen, setChosen] = useState(tables[0]);
  const [sources, setSources] = useState([]);
  const [showConfig, setShowConfig] = useState(false);
  const [draft, setDraft] = useState(null);
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const sourceKinds = [
    ['local-sqlite', '本地 SQLite（演示）'],
    ['aws-glue', 'AWS Glue Data Catalog'],
    ['trino', 'Trino / Hive Metastore'],
    ['postgresql', 'PostgreSQL / MySQL'],
  ];
  const loadSources = async () => {
    const response = await fetch('/api/data-sources');
    if (response.ok) setSources(await response.json());
  };
  useEffect(() => { loadSources(); }, []);
  const newSource = () => setDraft({ name: '', kind: 'aws-glue', endpoint: '', catalog_name: '', auth_type: 'read_only_account', credential: '', enabled: true });
  const selectSource = source => setDraft({ ...source, credential: '' });
  const saveSource = async event => {
    event.preventDefault(); if (!draft) return;
    setBusy(true); setNotice('');
    try {
      const response = await fetch(draft.id ? `/api/data-sources/${draft.id}` : '/api/data-sources', { method: draft.id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(draft) });
      const data = await response.json();
      if (!response.ok) throw Error(data.detail || '保存失败');
      setDraft({ ...data, credential: '' }); setNotice('数据源配置已保存。'); await loadSources();
    } catch (error) { setNotice(`保存失败：${error.message}`); } finally { setBusy(false); }
  };
  const runSourceAction = async action => {
    if (!draft?.id) return;
    setBusy(true); setNotice('');
    try {
      const response = await fetch(`/api/data-sources/${draft.id}/${action}`, { method: 'POST' });
      const data = await response.json();
      if (!response.ok) throw Error(data.detail || '操作失败');
      setNotice(action === 'sync' ? `已发现 ${data.table_count} 张表。技术元数据需要审核、补充语义后才会发布到 RAG。` : data.message || '连接测试成功。');
      await loadSources();
    } catch (error) { setNotice(`${action === 'sync' ? '采集' : '测试'}未完成：${error.message}`); } finally { setBusy(false); }
  };
  return <section className="page catalog-page">
    <div className="catalog-heading"><div><h1>数据目录</h1><p className="page-subtitle">维护语义元数据，并连接远端数据目录采集表结构。</p></div><button className="outline-button" onClick={() => { setShowConfig(!showConfig); if (!draft) setDraft(sources[0] ? { ...sources[0], credential: '' } : null); }}>数据源配置</button></div>
    {showConfig && <section className="source-config-panel">
      <aside className="source-list"><button className="add-source" onClick={newSource}>＋ 添加数据源</button>{sources.map(source => <button key={source.id} className={draft?.id === source.id ? 'active' : ''} onClick={() => selectSource(source)}><b>{source.name}</b><span>{source.kind} · {source.enabled ? '已启用' : '已停用'}</span></button>)}</aside>
      <form className="source-form" onSubmit={saveSource}>{draft ? <>
        <h2>{draft.id ? '编辑数据源' : '添加数据源'}</h2>
        <label>名称<input required value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} placeholder="例如：生产数仓" /></label>
        <label>连接类型<select value={draft.kind} onChange={event => setDraft({ ...draft, kind: event.target.value })}>{sourceKinds.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label>Endpoint / 地址<input value={draft.endpoint || ''} onChange={event => setDraft({ ...draft, endpoint: event.target.value })} placeholder="例如：https://glue.us-east-1.amazonaws.com 或 https://trino.example.com" /></label>
        <label>Catalog / 数据库<input value={draft.catalog_name || ''} onChange={event => setDraft({ ...draft, catalog_name: event.target.value })} placeholder="例如：ecommerce" /></label>
        <label>认证方式<select value={draft.auth_type} onChange={event => setDraft({ ...draft, auth_type: event.target.value })}><option value="none">无需认证</option><option value="read_only_account">只读账号 / Access Key</option><option value="token">访问令牌</option></select></label>
        <label>凭证<input type="password" value={draft.credential || ''} onChange={event => setDraft({ ...draft, credential: event.target.value })} placeholder={draft.credential_configured ? '留空保持已有凭证' : '仅保存加密后的凭证'} /></label>
        <label className="tool-check"><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />启用数据源</label>
        <div className="source-actions"><button disabled={busy}>保存</button>{draft.id && <><button type="button" className="outline-button" disabled={busy || !draft.enabled} onClick={() => runSourceAction('test')}>测试连接</button><button type="button" className="outline-button" disabled={busy || !draft.enabled} onClick={() => runSourceAction('sync')}>采集表结构</button></>}</div>
      </> : <p>点击“添加数据源”开始配置。</p>}</form>
      <p className="source-note">本地 SQLite 已接入，可真实采集。AWS Glue、Trino/Hive 与关系型数据库目前提供配置契约；需要在部署环境安装对应 SDK 或驱动后启用 Connector。</p>
    </section>}
    {notice && <p className="save-notice catalog-notice">{notice}</p>}
    <div className="catalog-layout"><div className="table-list">{tables.map(item => <button key={item.name} className={chosen.name === item.name ? 'active' : ''} onClick={() => setChosen(item)}><b>{item.display_name || item.name}</b><span>{item.name} · {item.description}</span></button>)}</div><div className="table-detail"><h2>{chosen.display_name || chosen.name} <small>{chosen.name}</small></h2><p>{chosen.description}</p>{chosen.data_grain && <p><b>数据粒度：</b>{chosen.data_grain}</p>}<p><b>主键：</b><code>{chosen.primary_key || chosen.primaryKey}</code></p><div className="field-list">{chosen.columns.map(column => { const [name, description] = Array.isArray(column) ? column : [column.name, column.description]; return <div key={name}><code>{name}</code><span>{description}{!Array.isArray(column) && <> <em>{column.type}</em></>}</span></div>; })}</div></div></div>
  </section>;
}
function ToolsPanel() {
  const [tools, setTools] = useState([]), [error, setError] = useState(''), [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    fetch('/api/tools', { signal: controller.signal })
      .then(async response => {
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.detail || '工具列表加载失败');
        if (!Array.isArray(payload)) throw new Error('工具接口返回格式不正确');
        return payload;
      })
      .then(payload => { if (active) { setTools(payload); setError(''); } })
      .catch(fetchError => { if (active && fetchError.name !== 'AbortError') { setTools([]); setError(fetchError.message); } })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; controller.abort(); };
  }, []);
  return <div className="extension-page"><h2>工具</h2><p className="page-subtitle">这里展示统一 Tool Registry 中的真实工具。工具是否可用还会同时受智能体绑定、用户权限和风险策略控制。</p>{loading && <p role="status">正在加载工具…</p>}{error && <p className="knowledge-page-error" role="alert">{error}</p>}<div className="extension-card-grid">{tools.map(tool => <article key={tool.id}><b>{tool.name}</b><code>{tool.id}</code><p>{tool.description}</p><div className="tool-meta"><span className={tool.risk_level}>风险：{tool.risk_level}</span><span>{tool.required_permission ? `权限：${tool.required_permission}` : '无需额外权限'}</span><span>{tool.enabled ? '已启用' : '已停用'}</span></div></article>)}</div></div>;
}
function SkillsPanel() {
  const [skills, setSkills] = useState([]), [notice, setNotice] = useState('');
  const load = () => fetch('/api/skills').then(response => response.json()).then(setSkills); useEffect(() => { load(); }, []);
  const save = async event => { event.preventDefault(); const form = new FormData(event.currentTarget); const response = await fetch('/api/skills', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ name:form.get('name'), description:form.get('description'), instructions:form.get('instructions'), enabled:true }) }); const data = await response.json(); setNotice(response.ok ? `Skill「${data.name}」已创建，可在智能体管理中绑定。` : data.detail || '创建失败'); if(response.ok) { event.currentTarget.reset(); load(); } };
  return <div className="extension-page"><h2>Skills</h2><p className="page-subtitle">Skill 是可复用的行为说明。绑定到某个智能体后，其内容会自动注入该智能体的 System Prompt。</p>{notice && <p className="save-notice catalog-notice">{notice}</p>}<div className="extension-split"><form className="config-form" onSubmit={save}><h2>新建 Skill</h2><input required name="name" placeholder="名称，例如：指标解释规范"/><input name="description" placeholder="简短说明"/><textarea required name="instructions" rows="8" placeholder={'写入这个 Skill 的执行规则，例如：\n回答 GMV 时必须说明公式、时间范围和退款处理方式。'}/><button>保存 Skill</button></form><div className="extension-items">{skills.length ? skills.map(skill => <article key={skill.id}><b>{skill.name}</b><span>{skill.description || '无说明'} · {skill.enabled ? '已启用' : '已停用'}</span></article>) : <p>还没有自定义 Skill。</p>}</div></div></div>;
}
function McpPanel() {
  const [servers, setServers] = useState([]), [notice, setNotice] = useState('');
  const load = () => fetch('/api/mcp-servers').then(response => response.json()).then(setServers); useEffect(() => { load(); }, []);
  const save = async event => { event.preventDefault(); const form = new FormData(event.currentTarget); const response = await fetch('/api/mcp-servers', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ name:form.get('name'), transport:'streamable-http', endpoint:form.get('endpoint'), api_key:form.get('api_key'), enabled:true }) }); const data = await response.json(); setNotice(response.ok ? 'MCP Server 已添加。' : data.detail || '添加失败'); if(response.ok) { event.currentTarget.reset(); load(); } };
  const test = async id => { const response = await fetch(`/api/mcp-servers/${id}/test`, {method:'POST'}); const data = await response.json(); setNotice(response.ok ? `连接成功，发现 ${data.tools.length} 个工具。` : data.detail || '测试失败'); load(); };
  return <div className="extension-page"><h2>MCP</h2><p className="page-subtitle">连接支持 Streamable HTTP 的 MCP Server。凭证会加密保存；“测试连接”会请求标准的 <code>tools/list</code>。</p>{notice && <p className="save-notice catalog-notice">{notice}</p>}<div className="extension-split"><form className="config-form" onSubmit={save}><h2>添加 MCP Server</h2><input required name="name" placeholder="名称，例如：内部指标服务"/><input required name="endpoint" placeholder="MCP Endpoint，例如：https://mcp.example.com/mcp"/><input name="api_key" type="password" placeholder="Bearer Token（可选）"/><button type="submit">保存 Server</button></form><div className="extension-items">{servers.length ? servers.map(server => <article key={server.id}><b>{server.name}</b><span>{server.transport} · {server.enabled ? '已启用' : '已停用'}</span><code>{server.endpoint}</code><small>{server.last_test_status || '尚未测试'}</small><button type="button" className="outline-button" onClick={() => test(server.id)}>测试连接</button></article>) : <p>还没有配置 MCP Server。</p>}</div></div></div>;
}
function ExtensionsPage({ canManage = false }) {
  const [tab, setTab] = useState('knowledge');
  const panel = tab === 'tools' ? <ToolsPanel /> : tab === 'mcp' ? <ManagedMcpPanel canManage={canManage} /> : tab === 'skills' ? <ManagedSkillsPanel canManage={canManage} /> : <KnowledgeBasePage />;
  return <section className="page"><h1>智能体扩展</h1><div className="extension-tabs" role="tablist"><button type="button" role="tab" aria-selected={tab==='knowledge'} className={tab==='knowledge'?'active':''} onClick={() => setTab('knowledge')}>知识库</button><button type="button" role="tab" aria-selected={tab==='tools'} className={tab==='tools'?'active':''} onClick={() => setTab('tools')}>工具</button><button type="button" role="tab" aria-selected={tab==='mcp'} className={tab==='mcp'?'active':''} onClick={() => setTab('mcp')}>MCP</button><button type="button" role="tab" aria-selected={tab==='skills'} className={tab==='skills'?'active':''} onClick={() => setTab('skills')}>Skills</button></div>{panel}</section>;
}
function Placeholder({ title, text }) { return <section className="page placeholder-page"><h1>{title}</h1><p>{text}</p></section>; }
function ManagementConsole() {
  const [tab,setTab]=useState('agents'),[agents,setAgents]=useState([]),[models,setModels]=useState([]),[providers,setProviders]=useState([]),[selected,setSelected]=useState(null),[notice,setNotice]=useState('');
  const load=()=>Promise.all([fetch('/api/agents').then(r=>r.json()),fetch('/api/models').then(r=>r.json()),fetch('/api/providers').then(r=>r.json())]).then(([a,m,p])=>{setAgents(a);setModels(m);setProviders(p);setSelected(x=>x||p[0])});
  useEffect(()=>{load()},[]);
  const updateProvider=async e=>{e.preventDefault();const f=new FormData(e.currentTarget);const body={name:f.get('name'),kind:f.get('kind'),base_url:f.get('base_url'),api_key:f.get('api_key'),enabled:f.get('enabled')==='on'};const r=await fetch(`/api/providers/${selected.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});setNotice(r.ok?'供应商配置已保存。':'保存失败。');if(r.ok)load()};
  const discover=async()=>{const r=await fetch(`/api/providers/${selected.id}/models/discover`);const d=await r.json();setNotice(d.models?.length?`发现模型：${d.models.join('、')}`:(d.message||'未发现模型，可手动添加。'))};
  const addModel=async e=>{e.preventDefault();const f=new FormData(e.currentTarget);const body={provider_id:selected.id,model_id:f.get('model_id'),display_name:f.get('display_name'),enabled:true};const r=await fetch('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});setNotice(r.ok?'模型已添加并启用。':'添加失败。');if(r.ok){e.currentTarget.reset();load()}};
  return <section className="page agent-page"><h1>智能体管理</h1><div className="manage-tabs"><button className={tab==='agents'?'active':''} onClick={()=>setTab('agents')}>智能体</button><button className={tab==='providers'?'active':''} onClick={()=>setTab('providers')}>模型供应商</button></div>{notice&&<p className="save-notice">{notice}</p>}{tab==='agents'?<div className="agent-manager"><aside className="agent-list">{agents.map(a=><button className={selected?.id===a.id?'active':''} key={a.id} onClick={()=>setSelected(a)}><b>{a.name}</b><span>{a.description}</span></button>)}</aside><div className="agent-editor"><h2>{selected?.name||'选择智能体'}</h2><p>{selected?.description}</p><p className="page-subtitle">身份与职责由平台固定；可编辑 Prompt、工具、MCP 和 Skills。</p></div></div>:<div className="provider-console"><aside className="provider-list">{providers.map(p=><button className={selected?.id===p.id?'active':''} key={p.id} onClick={()=>setSelected(p)}><b>{p.name}</b><span>{p.enabled?'已启用':'未启用'} · {p.kind}</span></button>)}</aside>{selected&&<div className="provider-detail"><form className="config-form" onSubmit={updateProvider}><h2>{selected.name}</h2><input name="name" defaultValue={selected.name}/><select name="kind" defaultValue={selected.kind}><option value="openai-compatible">OpenAI Compatible API</option><option value="anthropic-messages">Anthropic Messages API</option><option value="ollama">Ollama (Local)</option><option value="vllm">vLLM (Local)</option></select><input name="base_url" defaultValue={selected.base_url}/><input name="api_key" type="password" placeholder="留空则保留已有 Key；本地模型可为空"/><label className="tool-check"><input name="enabled" type="checkbox" defaultChecked={selected.enabled}/>启用供应商</label><button>保存供应商</button></form><div className="provider-models"><div><h2>模型</h2><button onClick={discover}>自动发现模型</button></div>{models.filter(m=>m.provider_id===selected.id).map(m=><p key={m.id}><b>{m.display_name}</b><span>{m.enabled?'已启用':'未启用'} · {m.model_id}</span></p>)}<form className="config-form" onSubmit={addModel}><h2>手动添加并启用模型</h2><input name="display_name" required placeholder="显示名称"/><input name="model_id" required placeholder="模型 ID"/><button>添加模型</button></form></div></div>}</div>}</section>;
}
function ManagementConsoleV2({ canManage = false }) {
  const [tab,setTab]=useState('agents'),[agents,setAgents]=useState([]),[models,setModels]=useState([]),[providers,setProviders]=useState([]),[tools,setTools]=useState([]),[skills,setSkills]=useState([]),[mcpServers,setMcpServers]=useState([]),[agentId,setAgentId]=useState(''),[providerId,setProviderId]=useState(''),[draft,setDraft]=useState(null),[providerDraft,setProviderDraft]=useState(null),[notice,setNotice]=useState('');
  const load=async()=>{try{const [a,m,p,t,s,mcps]=await Promise.all(['/api/agents','/api/models','/api/providers','/api/tools','/api/skills','/api/mcp-servers'].map(url=>fetchJson(url)));if(![a,m,p,t,s,mcps].every(Array.isArray))throw Error('管理接口返回格式不正确');setAgents(a);setModels(m);setProviders(p);setTools(t);setSkills(s);setMcpServers(mcps);setAgentId(x=>x||a[0]?.id||'');setProviderId(x=>x||p[0]?.id||'');setNotice('');}catch(error){setAgents([]);setModels([]);setProviders([]);setTools([]);setSkills([]);setMcpServers([]);setNotice(`管理配置加载失败：${error.message}`);}};
  useEffect(()=>{load()},[]); const agent=agents.find(a=>a.id===agentId); const provider=providers.find(p=>p.id===providerId);
  useEffect(()=>{if(agent)setDraft({...agent,mcp_servers:agent.mcp_servers||[],mcp_tools:agent.mcp_tools||[],skills:agent.skills||[]})},[agentId,agents]); useEffect(()=>{if(provider)setProviderDraft({...provider,api_key:'',connection_mode:provider.connection_mode||'direct',proxy_url:provider.proxy_url||''})},[providerId,providers]);
  const flip=(field,value)=>setDraft(x=>({...x,[field]:x[field].includes(value)?x[field].filter(v=>v!==value):[...x[field],value]}));
  const saveAgent=async e=>{e.preventDefault();const r=await fetch(`/api/agents/${draft.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({model_id:draft.model_id,tools:draft.tools,mcp_servers:draft.mcp_servers,mcp_tools:draft.mcp_tools,skills:draft.skills,system_prompt:draft.system_prompt,enabled:draft.enabled})});setNotice(r.ok?'智能体配置已保存。':'智能体保存失败。');if(r.ok)load()};
  const selectProvider=id=>{setProviderId(id);setProviderDraft(null)}; const newProvider=()=>{setProviderId('');setProviderDraft({name:'',kind:'openai-compatible',base_url:'',api_key:'',enabled:true,connection_mode:'direct',proxy_url:''})};
  const saveProvider=async e=>{e.preventDefault();const creating=!providerId;const url=creating?'/api/providers':`/api/providers/${providerId}`;const r=await fetch(url,{method:creating?'POST':'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(providerDraft)});const d=await r.json();setNotice(r.ok?'供应商配置已保存。':'供应商保存失败。');if(r.ok){setProviderId(d.id);load()}};
  const toggleModel=async m=>{await fetch(`/api/models/${m.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider_id:m.provider_id,model_id:m.model_id,display_name:m.display_name,enabled:!m.enabled})});load()};
  const addModel=async e=>{e.preventDefault();const f=new FormData(e.currentTarget);const r=await fetch('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider_id:providerId,model_id:f.get('model_id'),display_name:f.get('display_name'),enabled:true})});setNotice(r.ok?'模型已添加并启用。':'模型添加失败。');if(r.ok){e.currentTarget.reset();load()}};
  const discover=async()=>{const d=await fetch(`/api/providers/${providerId}/models/discover`).then(r=>r.json());setNotice(d.models?.length?`发现模型：${d.models.join('、')}`:(d.message||'未发现模型，可手动添加。'))};
  if(!canManage)return <section className="page"><h1>智能体管理</h1><p className="page-subtitle">仅平台管理员可以查看和修改智能体、模型供应商与扩展绑定配置。</p></section>;
  if((tab==='agents'&&!draft)||(tab==='providers'&&!providerDraft))return <section className="page"><h1>智能体管理</h1>{notice&&<p className="knowledge-page-error" role="alert">{notice}</p>}<p className="page-subtitle">正在加载管理配置…</p></section>;
  const compatibleTools=tools.filter(tool=>!tool.allowed_agents?.length||tool.allowed_agents.includes(draft?.id));
  return <section className="page agent-page"><h1>智能体管理</h1><div className="manage-tabs"><button className={tab==='agents'?'active':''} onClick={()=>setTab('agents')}>智能体</button><button className={tab==='providers'?'active':''} onClick={()=>setTab('providers')}>模型供应商</button></div>{tab==='agents'?<div className="agent-manager"><aside className="agent-list">{agents.map(a=><button className={a.id===agentId?'active':''} key={a.id} onClick={()=>setAgentId(a.id)}><b>{a.name}</b><span>{a.description}</span></button>)}</aside><form className="agent-editor" onSubmit={saveAgent}><div className="editor-heading"><div><h2>{draft.name}</h2><p>{draft.description}</p></div><label className="switch-label"><input type="checkbox" checked={draft.enabled} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>启用</label></div><label>绑定模型<select value={draft.model_id} onChange={e=>setDraft({...draft,model_id:e.target.value})}>{models.filter(m=>m.enabled).map(m=><option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label><label>System Prompt<textarea rows="7" value={draft.system_prompt} onChange={e=>setDraft({...draft,system_prompt:e.target.value})}/></label><fieldset><legend>内置工具</legend>{compatibleTools.filter(tool=>tool.category!=='mcp').map(tool=><label className="tool-check" key={tool.id}><input type="checkbox" checked={draft.tools.includes(tool.id)} onChange={()=>flip('tools',tool.id)}/>{tool.name}<small>{tool.risk_level==='high'?' · 高风险，调用时需审批':''}</small></label>)}</fieldset><fieldset><legend>MCP Server</legend>{mcpServers.filter(item=>item.enabled).map(item=><label className="tool-check" key={item.id}><input type="checkbox" checked={draft.mcp_servers.includes(item.id)} onChange={()=>flip('mcp_servers',item.id)}/>{item.name} <small>· {item.tool_count||0} 个工具</small></label>)}</fieldset><fieldset><legend>单独绑定 MCP 工具（可选）</legend>{tools.filter(tool=>tool.category==='mcp').map(tool=><label className="tool-check" key={tool.id}><input type="checkbox" checked={draft.mcp_tools.includes(tool.id)} onChange={()=>flip('mcp_tools',tool.id)}/>{tool.name}</label>)}</fieldset><fieldset><legend>Skills</legend>{skills.filter(item=>item.enabled).map(item=><label className="tool-check" key={item.id}><input type="checkbox" checked={draft.skills.includes(item.id)} onChange={()=>flip('skills',item.id)}/>{item.name}<small> · v{item.current_version}{item.always_on?' · 始终注入':' · 按需选择'}</small></label>)}</fieldset><button className="save-agent">保存配置</button></form></div>:<div className="provider-console"><aside className="provider-list"><button className="add-provider" onClick={newProvider}>＋ 添加供应商</button>{providers.map(p=><button className={p.id===providerId?'active':''} key={p.id} onClick={()=>selectProvider(p.id)}><b>{p.name}</b><span>{p.enabled?'已启用':'未启用'} · {p.kind}</span></button>)}</aside><div className="provider-detail"><form className="config-form" onSubmit={saveProvider}><h2>{providerId?'编辑供应商':'添加供应商'}</h2><input required value={providerDraft.name} onChange={e=>setProviderDraft({...providerDraft,name:e.target.value})} placeholder="供应商名称"/><select value={providerDraft.kind} onChange={e=>setProviderDraft({...providerDraft,kind:e.target.value})}><option value="openai-compatible">OpenAI Compatible API</option><option value="anthropic-messages">Anthropic Messages API</option><option value="ollama">Ollama (Local)</option><option value="vllm">vLLM (Local)</option></select><input required value={providerDraft.base_url} onChange={e=>setProviderDraft({...providerDraft,base_url:e.target.value})} placeholder="Base URL"/><input type="password" value={providerDraft.api_key} onChange={e=>setProviderDraft({...providerDraft,api_key:e.target.value})} placeholder="API Key（留空保留原值；本地可空）"/><label className="tool-check"><input type="checkbox" checked={providerDraft.enabled} onChange={e=>setProviderDraft({...providerDraft,enabled:e.target.checked})}/>启用供应商</label><button>保存供应商</button></form>{providerId&&<div className="provider-models"><div><h2>该供应商的模型</h2><button onClick={discover}>自动发现</button></div>{models.filter(m=>m.provider_id===providerId).map(m=><p key={m.id}><b>{m.display_name}</b><span>{m.model_id}</span><button onClick={()=>toggleModel(m)}>{m.enabled?'已启用':'未启用'}</button></p>)}<form className="config-form" onSubmit={addModel}><h2>手动添加模型</h2><input name="display_name" required placeholder="显示名称"/><input name="model_id" required placeholder="模型 ID"/><button>添加并启用</button></form></div>}</div></div>}{notice&&<p className="save-notice">{notice}</p>}</section>;
}
function AgentManager({ canManage = false }) {
  return <ManagementConsoleV2 canManage={canManage} />;
  const [tab,setTab]=useState('agents'),[agents,setAgents]=useState([]),[models,setModels]=useState([]),[providers,setProviders]=useState([]),[selectedId,setSelectedId]=useState(''),[draft,setDraft]=useState(null),[notice,setNotice]=useState('');
  const load=()=>Promise.all([fetch('/api/agents').then(r=>r.json()),fetch('/api/models').then(r=>r.json()),fetch('/api/providers').then(r=>r.json())]).then(([a,m,p])=>{setAgents(a);setModels(m);setProviders(p);setSelectedId(x=>x||a[0]?.id||'')});
  useEffect(()=>{load()},[]); const selected=agents.find(x=>x.id===selectedId); useEffect(()=>{if(selected)setDraft({...selected,mcp_servers:selected.mcp_servers||[],skills:selected.skills||[]})},[selectedId,agents]);
  const flip=(field,value)=>setDraft(x=>({...x,[field]:x[field].includes(value)?x[field].filter(v=>v!==value):[...x[field],value]}));
  const save=async e=>{e.preventDefault();const r=await fetch(`/api/agents/${draft.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({model_id:draft.model_id,tools:draft.tools,mcp_servers:draft.mcp_servers,skills:draft.skills,system_prompt:draft.system_prompt,enabled:draft.enabled})});setNotice(r.ok?'配置已保存。':'保存失败。');if(r.ok)load()};
  const addProvider=async e=>{e.preventDefault();const form=new FormData(e.currentTarget);const payload=Object.fromEntries(form);payload.enabled=true;const r=await fetch('/api/providers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});setNotice(r.ok?'供应商已添加，请继续添加并启用模型。':'供应商添加失败。');if(r.ok){e.currentTarget.reset();load()}};
  const addModel=async e=>{e.preventDefault();const form=new FormData(e.currentTarget);const payload=Object.fromEntries(form);payload.enabled=true;const r=await fetch('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});setNotice(r.ok?'模型已启用，可在智能体中选择。':'模型添加失败。');if(r.ok){e.currentTarget.reset();load()}};
  if(!draft)return <section className="page"><h1>智能体管理</h1></section>;
  return <section className="page agent-page"><h1>智能体管理</h1><div className="manage-tabs"><button className={tab==='agents'?'active':''} onClick={()=>setTab('agents')}>智能体</button><button className={tab==='providers'?'active':''} onClick={()=>setTab('providers')}>模型供应商</button></div>{notice&&<p className="save-notice">{notice}</p>}{tab==='agents'?<div className="agent-manager"><aside className="agent-list">{agents.map(a=><button className={a.id===selectedId?'active':''} key={a.id} onClick={()=>setSelectedId(a.id)}><b>{a.name}</b><span>{a.description}</span></button>)}</aside><form className="agent-editor" onSubmit={save}><div className="editor-heading"><div><h2>{draft.name}</h2><p>{draft.description}</p></div><label className="switch-label"><input type="checkbox" checked={draft.enabled} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>启用</label></div><label>绑定模型<select value={draft.model_id} onChange={e=>setDraft({...draft,model_id:e.target.value})}>{models.filter(m=>m.enabled).map(m=><option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label><label>System Prompt<textarea rows="7" value={draft.system_prompt} onChange={e=>setDraft({...draft,system_prompt:e.target.value})}/></label><fieldset><legend>工具</legend>{['chat','catalog_rag','readonly_sql','query_result_analysis','etl','memory_search','memory_write','memory_forget'].map(x=><label className="tool-check" key={x}><input type="checkbox" checked={draft.tools.includes(x)} onChange={()=>flip('tools',x)}/>{x}</label>)}</fieldset><fieldset><legend>MCP</legend>{['catalog-mcp','query-mcp','etl-mcp'].map(x=><label className="tool-check" key={x}><input type="checkbox" checked={draft.mcp_servers.includes(x)} onChange={()=>flip('mcp_servers',x)}/>{x}</label>)}</fieldset><fieldset><legend>Skills</legend>{['sql-analysis','metric-interpretation','data-quality'].map(x=><label className="tool-check" key={x}><input type="checkbox" checked={draft.skills.includes(x)} onChange={()=>flip('skills',x)}/>{x}</label>)}</fieldset><button className="save-agent">保存配置</button></form></div>:<div className="provider-console"><div className="provider-grid">{providers.map(p=><article className="provider-card" key={p.id}><div><b>{p.name}</b><em>{p.enabled?'已启用':'未启用'}</em></div><small>{p.kind}</small><code>{p.base_url}</code><span>Key {p.api_key_mask||'未设置'}</span></article>)}</div><div className="provider-forms"><form className="config-form" onSubmit={addProvider}><h2>添加供应商</h2><input name="name" required placeholder="名称，如 DeepSeek / 本地 vLLM"/><select name="kind"><option value="openai-compatible">OpenAI Compatible</option><option value="ollama">Ollama (Local)</option><option value="vllm">vLLM (Local)</option></select><input name="base_url" required placeholder="Base URL，如 http://localhost:11434/v1"/><input name="api_key" type="password" placeholder="API Key（本地模型可留空）"/><button>保存供应商</button></form><form className="config-form" onSubmit={addModel}><h2>添加并启用模型</h2><select name="provider_id" required><option value="">选择供应商</option>{providers.filter(p=>p.enabled).map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select><input name="model_id" required placeholder="模型 ID，如 deepseek-v4-flash"/><input name="display_name" required placeholder="显示名称"/><button>启用模型</button></form></div><div className="model-list">{models.map(m=><div key={m.id}><b>{m.display_name}</b><span>{m.enabled?'已启用':'未启用'} · {m.model_id}</span></div>)}</div></div>}</section>;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const raw = await response.text();
    let detail = '';
    try { detail = raw ? JSON.parse(raw).detail : ''; } catch { detail = raw; }
    throw Error(detail || `请求失败（HTTP ${response.status}）`);
  }
  return response.json();
}

function storedMessageToUi(message) {
  if (message.role === 'tool' || message.kind === 'tool') return null;
  if (message.role === 'user') {
    return { id: message.id, role: 'user', text: message.content, clientMessageId: message.client_message_id, createdAt: message.created_at };
  }
  if (message.role !== 'assistant' && message.role !== 'error') return null;
  let payload = message.payload;
  if (typeof payload === 'string') {
    try { payload = JSON.parse(payload); } catch { payload = {}; }
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) payload = {};
  const isError = message.role === 'error' || message.kind === 'error';
  return {
    id: message.id,
    role: 'assistant',
    createdAt: message.created_at,
    data: { ...payload, intent: payload.intent || (isError ? 'ERROR' : 'CHAT'), answer: payload.answer || payload.detail || message.content || '' },
  };
}

function Workspace({ user, onLogout }) {
  const [view, setView] = useState('new-chat');
  const [conversations, setConversations] = useState([]);
  const [activeId, setActiveId] = useState('');
  const [recentOpen, setRecentOpen] = useState(true);
  const [initialLoading, setInitialLoading] = useState(true);
  const [messageLoading, setMessageLoading] = useState(false);
  const [listError, setListError] = useState('');
  const [messageError, setMessageError] = useState('');
  const mounted = useRef(true);
  const messageRequest = useRef(0);
  const active = conversations.find(item => item.id === activeId) || null;

  const loadConversationMessages = async (conversationId, signal) => {
    const request = ++messageRequest.current;
    setMessageLoading(true); setMessageError('');
    try {
      const data = await fetchJson(`/api/conversations/${conversationId}/messages`, { signal });
      if (!mounted.current || request !== messageRequest.current) return;
      const messages = (data.messages || []).map(storedMessageToUi).filter(Boolean);
      setConversations(items => items.map(item => item.id === conversationId ? { ...item, ...data.conversation, messages } : item));
    } catch (error) {
      if (error.name !== 'AbortError' && mounted.current && request === messageRequest.current) setMessageError(`对话内容加载失败：${error.message}`);
    } finally {
      if (mounted.current && request === messageRequest.current) setMessageLoading(false);
    }
  };

  const refreshConversationSummaries = async () => {
    try {
      const summaries = await fetchJson('/api/conversations');
      if (!mounted.current) return;
      setConversations(items => {
        const merged = summaries.map(summary => ({ ...summary, messages: items.find(item => item.id === summary.id)?.messages ?? null }));
        const activeLocal = items.find(item => item.id === activeId);
        if (activeLocal && !merged.some(item => item.id === activeLocal.id)) merged.unshift(activeLocal);
        return merged;
      });
      setListError('');
    } catch (error) {
      if (mounted.current) setListError(`最近对话刷新失败：${error.message}`);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    const initialize = async () => {
      setInitialLoading(true); setListError('');
      try {
        let summaries = await fetchJson('/api/conversations', { signal: controller.signal });
        if (!summaries.length) {
          const created = await fetchJson('/api/conversations', { method: 'POST', signal: controller.signal });
          summaries = [created];
        }
        if (!mounted.current) return;
        setConversations(summaries.map(summary => ({ ...summary, messages: null })));
        setActiveId(summaries[0].id);
        await loadConversationMessages(summaries[0].id, controller.signal);
      } catch (error) {
        if (error.name !== 'AbortError' && mounted.current) setListError(`对话列表加载失败：${error.message}`);
      } finally {
        if (mounted.current) setInitialLoading(false);
      }
    };
    initialize();
    return () => { mounted.current = false; messageRequest.current += 1; controller.abort(); };
  }, []);

  const createConversation = async () => {
    setListError(''); setMessageError('');
    try {
      const summary = await fetchJson('/api/conversations', { method: 'POST' });
      if (!mounted.current) return;
      setConversations(items => [{ ...summary, messages: [] }, ...items.filter(item => item.id !== summary.id)]);
      setActiveId(summary.id); setMessageLoading(false); setView('new-chat');
    } catch (error) {
      if (mounted.current) setListError(`创建新对话失败：${error.message}`);
    }
  };
  const selectConversation = conversationId => {
    setActiveId(conversationId); setView('new-chat');
    loadConversationMessages(conversationId);
  };
  const updateMessages = (conversationId, updater) => setConversations(items => items.map(item => {
    if (item.id !== conversationId) return item;
    const currentMessages = item.messages || [];
    return { ...item, messages: typeof updater === 'function' ? updater(currentMessages) : updater, updated_at: new Date().toISOString() };
  }));
  const setTitle = (conversationId, text) => setConversations(items => items.map(item => item.id === conversationId ? { ...item, title: item.title === '新对话' ? text.slice(0, 18) : item.title } : item));
  const clearAndLogout = () => {
    messageRequest.current += 1; setConversations([]); setActiveId(''); setMessageError(''); setListError('');
    onLogout();
  };
  const sorted = [...conversations].sort((a, b) => new Date(b.updated_at || 0) - new Date(a.updated_at || 0));
  const visibleNav = user.is_catalog_admin ? [...nav, ['system', '▣', '系统管理']] : nav;

  let content;
  if (view === 'new-chat') {
    if (initialLoading || messageLoading) content = <section className="chat-page"><div className="home-welcome"><h1>正在加载对话…</h1></div></section>;
    else if (messageError) content = <section className="chat-page"><div className="home-welcome"><h1>对话加载失败</h1><p className="auth-error">{messageError}</p>{active && <button className="save-agent" onClick={() => loadConversationMessages(active.id)}>重试</button>}</div></section>;
    else if (active) content = <ChatHome key={active.id} session={active.id} messages={active.messages || []} setMessages={updater => updateMessages(active.id, updater)} onFirstMessage={text => setTitle(active.id, text)} onNewConversation={createConversation} onConversationChanged={refreshConversationSummaries} />;
    else content = <section className="chat-page"><div className="home-welcome"><h1>暂时无法加载对话</h1>{listError && <p className="auth-error">{listError}</p>}</div></section>;
  } else if (view === 'catalog') content = <CatalogPage isCatalogAdmin={Boolean(user.is_catalog_admin)} />;
  else if (view === 'extensions') content = <ExtensionsPage canManage={Boolean(user.is_catalog_admin)} />;
  else if (view === 'system' && user.is_catalog_admin) content = <SystemManagementPage currentUser={user} />;
  else content = <AgentManager canManage={Boolean(user.is_catalog_admin)} />;

  return <div className="console-shell"><aside className="console-sidebar"><div className="console-brand">✦ Data Agent</div><nav>{visibleNav.map(([id, icon, label]) => <button key={id} className={view === id ? 'active' : ''} onClick={() => id === 'new-chat' ? createConversation() : setView(id)}><span>{icon}</span>{label}</button>)}</nav><div className="recent-block"><button className="recent-toggle" onClick={() => setRecentOpen(open => !open)}>最近 <span>{recentOpen ? '⌃' : '⌄'}</span></button>{recentOpen && <>{initialLoading ? <small>正在加载最近对话…</small> : <>{sorted.map(item => <button className={`recent-item ${activeId === item.id && view === 'new-chat' ? 'active' : ''}`} key={item.id} onClick={() => selectConversation(item.id)}>{item.title}</button>)}{listError ? <small className="recent-error">{listError}</small> : !sorted.length && <small>暂无最近对话</small>}</>}</>}</div><div className="user-id"><span>●</span><div><b>{user.username}</b><small>{user.is_catalog_admin ? '平台管理员' : '数据分析师'}</small></div><button className="logout-button" onClick={clearAndLogout}>退出</button></div></aside><main>{content}</main></div>;
}
class AppErrorBoundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  componentDidCatch(error, info) { console.error('Data Agent UI crashed', error, info); }
  render() {
    if (!this.state.error) return this.props.children;
    return <div className="auth-shell"><div className="auth-card"><div className="console-brand">✦ Data Agent</div><h1>页面暂时无法显示</h1><p>某个页面组件发生异常。你可以返回工作台，错误详情已保留在浏览器控制台。</p><button type="button" onClick={() => { window.history.replaceState(null, '', '/'); window.location.reload(); }}>返回工作台</button></div></div>;
  }
}
function App() {
  const [user, setUser] = useState(undefined), [mode, setMode] = useState('login'), [error, setError] = useState('');
  useEffect(() => {
    const restoreAppPath = () => { if (window.location.pathname !== '/') window.history.replaceState(null, '', '/'); };
    restoreAppPath(); window.addEventListener('popstate', restoreAppPath);
    fetch('/api/auth/me').then(response => response.ok ? response.json() : null).then(setUser).catch(() => setUser(null));
    return () => window.removeEventListener('popstate', restoreAppPath);
  }, []);
  const authenticate = async event => { event.preventDefault(); const form = new FormData(event.currentTarget); setError(''); const response = await fetch(`/api/auth/${mode}`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ username:form.get('username'), password:form.get('password') }) }); const data = await response.json(); if (!response.ok) { setError(data.detail || '操作失败'); return; } setUser(data); };
  const logout = async () => { await fetch('/api/auth/logout', {method:'POST'}); setUser(null); setMode('login'); };
  if (user === undefined) return <div className="auth-shell">正在加载…</div>;
  if (user) return <Workspace user={user} onLogout={logout} />;
  return <div className="auth-shell"><form className="auth-card" onSubmit={authenticate}><div className="console-brand">✦ Data Agent</div><h1>{mode === 'login' ? '登录' : '创建账号'}</h1><p>{mode === 'login' ? '登录后继续使用你的数据分析工作台。' : '创建本地账号，保存个人对话和知识库。'}</p><label>用户名<input required name="username" minLength="3" pattern="[A-Za-z0-9_-]+" placeholder="3–40 位字母、数字或 _ -" /></label><label>密码<input required name="password" type="password" minLength="8" placeholder="至少 8 位" /></label>{error && <div className="auth-error">{error}</div>}<button>{mode === 'login' ? '登录' : '注册并登录'}</button><span> {mode === 'login' ? '还没有账号？' : '已有账号？'} <a onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError(''); }}>{mode === 'login' ? '注册' : '登录'}</a></span></form></div>;
}
createRoot(document.getElementById('root')).render(<AppErrorBoundary><App /></AppErrorBoundary>);
