import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import catalog from '../metadata/catalog.json';
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
    <p>{data.answer}</p>
    {data.sql && <details><summary>查看 SQL</summary><pre>{data.sql}</pre></details>}
    {columns.length > 0 && <div className="table-wrap"><table><thead><tr>{columns.map(x => <th key={x}>{x}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{columns.map(x => <td key={x}>{row[x]}</td>)}</tr>)}</tbody></table></div>}
  </article>;
}

function Composer({ models, modelId, setModelId, onSend, busy }) {
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
      <select aria-label="选择模型" value={modelId} onChange={e => setModelId(e.target.value)}>
        <option value="">默认模型</option>
        {models.filter(model => model.enabled).map(model => <option key={model.id} value={model.id}>{model.display_name}</option>)}
      </select>
      <button type="submit" disabled={busy || !value.trim()} aria-label="发送">↑</button>
    </div>
  </form>;
}

function ChatHome({ session, messages, setMessages, onFirstMessage, onNewConversation }) {
  const [models, setModels] = useState([]);
  const [modelId, setModelId] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => { fetch('/api/models').then(r => r.json()).then(modelData => { setModels(modelData); setModelId(modelData[0]?.id || ''); }); }, []);
  const send = async text => {
    const message = text.trim(); if (!message || busy) return;
    setBusy(true); setMessages(items => [...items, { role: 'user', text: message }]); onFirstMessage(message);
    try {
      const response = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message, session_id: session, model_id: modelId || null }) });
      const raw = await response.text(); const data = JSON.parse(raw);
      if (!response.ok) throw Error(data.detail || '请求失败');
      setMessages(items => [...items, { role: 'assistant', data }]);
    } catch (error) { setMessages(items => [...items, { role: 'assistant', data: { intent: 'ERROR', answer: error.message } }]); }
    finally { setBusy(false); }
  };
  return <section className="chat-page">
    {messages.length === 0 ? <div className="home-welcome"><h1>你好，有什么可以帮助你的？</h1><Composer {...{ models, modelId, setModelId, onSend: send, busy }} /></div> : <><div className="conversation-list">{messages.map((item, index) => item.role === 'user' ? <div className="user-bubble" key={index}>{item.text}</div> : <AssistantAnswer key={index} data={item.data} />)}</div><div className="chat-docked"><Composer {...{ models, modelId, setModelId, onSend: send, busy }} /></div><button className="new-thread-small" onClick={onNewConversation}>新对话</button></>}
  </section>;
}

function Catalog() { const [chosen, setChosen] = useState(catalog[0]); return <section className="page"><h1>数据目录</h1><p className="page-subtitle">查看已登记的数据资产、字段与关联关系。</p><div className="catalog-layout"><div className="table-list">{catalog.map(item => <button key={item.name} className={chosen.name === item.name ? 'active' : ''} onClick={() => setChosen(item)}><b>{item.name}</b><span>{item.description}</span></button>)}</div><div className="table-detail"><h2>{chosen.name}</h2><p>{chosen.description}</p><div className="field-list">{chosen.columns.map(([name, description]) => <div key={name}><code>{name}</code><span>{description}</span></div>)}</div></div></div></section>; }
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
function ManagementConsoleV2() {
  const [tab,setTab]=useState('agents'),[agents,setAgents]=useState([]),[models,setModels]=useState([]),[providers,setProviders]=useState([]),[agentId,setAgentId]=useState(''),[providerId,setProviderId]=useState(''),[draft,setDraft]=useState(null),[providerDraft,setProviderDraft]=useState(null),[notice,setNotice]=useState('');
  const load=()=>Promise.all([fetch('/api/agents').then(r=>r.json()),fetch('/api/models').then(r=>r.json()),fetch('/api/providers').then(r=>r.json())]).then(([a,m,p])=>{setAgents(a);setModels(m);setProviders(p);setAgentId(x=>x||a[0]?.id||'');setProviderId(x=>x||p[0]?.id||'')});
  useEffect(()=>{load()},[]); const agent=agents.find(a=>a.id===agentId); const provider=providers.find(p=>p.id===providerId);
  useEffect(()=>{if(agent)setDraft({...agent,mcp_servers:agent.mcp_servers||[],skills:agent.skills||[]})},[agentId,agents]); useEffect(()=>{if(provider)setProviderDraft({...provider,api_key:''})},[providerId,providers]);
  const flip=(field,value)=>setDraft(x=>({...x,[field]:x[field].includes(value)?x[field].filter(v=>v!==value):[...x[field],value]}));
  const saveAgent=async e=>{e.preventDefault();const r=await fetch(`/api/agents/${draft.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({model_id:draft.model_id,tools:draft.tools,mcp_servers:draft.mcp_servers,skills:draft.skills,system_prompt:draft.system_prompt,enabled:draft.enabled})});setNotice(r.ok?'智能体配置已保存。':'智能体保存失败。');if(r.ok)load()};
  const selectProvider=id=>{setProviderId(id);setProviderDraft(null)}; const newProvider=()=>{setProviderId('');setProviderDraft({name:'',kind:'openai-compatible',base_url:'',api_key:'',enabled:true})};
  const saveProvider=async e=>{e.preventDefault();const creating=!providerId;const url=creating?'/api/providers':`/api/providers/${providerId}`;const r=await fetch(url,{method:creating?'POST':'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(providerDraft)});const d=await r.json();setNotice(r.ok?'供应商配置已保存。':'供应商保存失败。');if(r.ok){setProviderId(d.id);load()}};
  const toggleModel=async m=>{await fetch(`/api/models/${m.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider_id:m.provider_id,model_id:m.model_id,display_name:m.display_name,enabled:!m.enabled})});load()};
  const addModel=async e=>{e.preventDefault();const f=new FormData(e.currentTarget);const r=await fetch('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({provider_id:providerId,model_id:f.get('model_id'),display_name:f.get('display_name'),enabled:true})});setNotice(r.ok?'模型已添加并启用。':'模型添加失败。');if(r.ok){e.currentTarget.reset();load()}};
  const discover=async()=>{const d=await fetch(`/api/providers/${providerId}/models/discover`).then(r=>r.json());setNotice(d.models?.length?`发现模型：${d.models.join('、')}`:(d.message||'未发现模型，可手动添加。'))};
  if(!draft||!providerDraft)return <section className="page"><h1>智能体管理</h1></section>;
  return <section className="page agent-page"><h1>智能体管理</h1><div className="manage-tabs"><button className={tab==='agents'?'active':''} onClick={()=>setTab('agents')}>智能体</button><button className={tab==='providers'?'active':''} onClick={()=>setTab('providers')}>模型供应商</button></div>{tab==='agents'?<div className="agent-manager"><aside className="agent-list">{agents.map(a=><button className={a.id===agentId?'active':''} key={a.id} onClick={()=>setAgentId(a.id)}><b>{a.name}</b><span>{a.description}</span></button>)}</aside><form className="agent-editor" onSubmit={saveAgent}><div className="editor-heading"><div><h2>{draft.name}</h2><p>{draft.description}</p></div><label className="switch-label"><input type="checkbox" checked={draft.enabled} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>启用</label></div><label>绑定模型<select value={draft.model_id} onChange={e=>setDraft({...draft,model_id:e.target.value})}>{models.filter(m=>m.enabled).map(m=><option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label><label>System Prompt<textarea rows="7" value={draft.system_prompt} onChange={e=>setDraft({...draft,system_prompt:e.target.value})}/></label><fieldset><legend>工具</legend>{['chat','catalog_rag','readonly_sql','query_result_analysis','etl'].map(x=><label className="tool-check" key={x}><input type="checkbox" checked={draft.tools.includes(x)} onChange={()=>flip('tools',x)}/>{x}</label>)}</fieldset><fieldset><legend>MCP</legend>{['catalog-mcp','query-mcp','etl-mcp'].map(x=><label className="tool-check" key={x}><input type="checkbox" checked={draft.mcp_servers.includes(x)} onChange={()=>flip('mcp_servers',x)}/>{x}</label>)}</fieldset><fieldset><legend>Skills</legend>{['sql-analysis','metric-interpretation','data-quality'].map(x=><label className="tool-check" key={x}><input type="checkbox" checked={draft.skills.includes(x)} onChange={()=>flip('skills',x)}/>{x}</label>)}</fieldset><button className="save-agent">保存配置</button></form></div>:<div className="provider-console"><aside className="provider-list"><button className="add-provider" onClick={newProvider}>＋ 添加供应商</button>{providers.map(p=><button className={p.id===providerId?'active':''} key={p.id} onClick={()=>selectProvider(p.id)}><b>{p.name}</b><span>{p.enabled?'已启用':'未启用'} · {p.kind}</span></button>)}</aside><div className="provider-detail"><form className="config-form" onSubmit={saveProvider}><h2>{providerId?'编辑供应商':'添加供应商'}</h2><input required value={providerDraft.name} onChange={e=>setProviderDraft({...providerDraft,name:e.target.value})} placeholder="供应商名称"/><select value={providerDraft.kind} onChange={e=>setProviderDraft({...providerDraft,kind:e.target.value})}><option value="openai-compatible">OpenAI Compatible API</option><option value="anthropic-messages">Anthropic Messages API</option><option value="ollama">Ollama (Local)</option><option value="vllm">vLLM (Local)</option></select><input required value={providerDraft.base_url} onChange={e=>setProviderDraft({...providerDraft,base_url:e.target.value})} placeholder="Base URL"/><input type="password" value={providerDraft.api_key} onChange={e=>setProviderDraft({...providerDraft,api_key:e.target.value})} placeholder="API Key（留空保留原值；本地可空）"/><label className="tool-check"><input type="checkbox" checked={providerDraft.enabled} onChange={e=>setProviderDraft({...providerDraft,enabled:e.target.checked})}/>启用供应商</label><button>保存供应商</button></form>{providerId&&<div className="provider-models"><div><h2>该供应商的模型</h2><button onClick={discover}>自动发现</button></div>{models.filter(m=>m.provider_id===providerId).map(m=><p key={m.id}><b>{m.display_name}</b><span>{m.model_id}</span><button onClick={()=>toggleModel(m)}>{m.enabled?'已启用':'未启用'}</button></p>)}<form className="config-form" onSubmit={addModel}><h2>手动添加模型</h2><input name="display_name" required placeholder="显示名称"/><input name="model_id" required placeholder="模型 ID"/><button>添加并启用</button></form></div>}</div></div>}{notice&&<p className="save-notice">{notice}</p>}</section>;
}
function AgentManager() {
  return <ManagementConsoleV2 />;
  const [tab,setTab]=useState('agents'),[agents,setAgents]=useState([]),[models,setModels]=useState([]),[providers,setProviders]=useState([]),[selectedId,setSelectedId]=useState(''),[draft,setDraft]=useState(null),[notice,setNotice]=useState('');
  const load=()=>Promise.all([fetch('/api/agents').then(r=>r.json()),fetch('/api/models').then(r=>r.json()),fetch('/api/providers').then(r=>r.json())]).then(([a,m,p])=>{setAgents(a);setModels(m);setProviders(p);setSelectedId(x=>x||a[0]?.id||'')});
  useEffect(()=>{load()},[]); const selected=agents.find(x=>x.id===selectedId); useEffect(()=>{if(selected)setDraft({...selected,mcp_servers:selected.mcp_servers||[],skills:selected.skills||[]})},[selectedId,agents]);
  const flip=(field,value)=>setDraft(x=>({...x,[field]:x[field].includes(value)?x[field].filter(v=>v!==value):[...x[field],value]}));
  const save=async e=>{e.preventDefault();const r=await fetch(`/api/agents/${draft.id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({model_id:draft.model_id,tools:draft.tools,mcp_servers:draft.mcp_servers,skills:draft.skills,system_prompt:draft.system_prompt,enabled:draft.enabled})});setNotice(r.ok?'配置已保存。':'保存失败。');if(r.ok)load()};
  const addProvider=async e=>{e.preventDefault();const form=new FormData(e.currentTarget);const payload=Object.fromEntries(form);payload.enabled=true;const r=await fetch('/api/providers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});setNotice(r.ok?'供应商已添加，请继续添加并启用模型。':'供应商添加失败。');if(r.ok){e.currentTarget.reset();load()}};
  const addModel=async e=>{e.preventDefault();const form=new FormData(e.currentTarget);const payload=Object.fromEntries(form);payload.enabled=true;const r=await fetch('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});setNotice(r.ok?'模型已启用，可在智能体中选择。':'模型添加失败。');if(r.ok){e.currentTarget.reset();load()}};
  if(!draft)return <section className="page"><h1>智能体管理</h1></section>;
  return <section className="page agent-page"><h1>智能体管理</h1><div className="manage-tabs"><button className={tab==='agents'?'active':''} onClick={()=>setTab('agents')}>智能体</button><button className={tab==='providers'?'active':''} onClick={()=>setTab('providers')}>模型供应商</button></div>{notice&&<p className="save-notice">{notice}</p>}{tab==='agents'?<div className="agent-manager"><aside className="agent-list">{agents.map(a=><button className={a.id===selectedId?'active':''} key={a.id} onClick={()=>setSelectedId(a.id)}><b>{a.name}</b><span>{a.description}</span></button>)}</aside><form className="agent-editor" onSubmit={save}><div className="editor-heading"><div><h2>{draft.name}</h2><p>{draft.description}</p></div><label className="switch-label"><input type="checkbox" checked={draft.enabled} onChange={e=>setDraft({...draft,enabled:e.target.checked})}/>启用</label></div><label>绑定模型<select value={draft.model_id} onChange={e=>setDraft({...draft,model_id:e.target.value})}>{models.filter(m=>m.enabled).map(m=><option key={m.id} value={m.id}>{m.display_name}</option>)}</select></label><label>System Prompt<textarea rows="7" value={draft.system_prompt} onChange={e=>setDraft({...draft,system_prompt:e.target.value})}/></label><fieldset><legend>工具</legend>{['chat','catalog_rag','readonly_sql','query_result_analysis','etl'].map(x=><label className="tool-check" key={x}><input type="checkbox" checked={draft.tools.includes(x)} onChange={()=>flip('tools',x)}/>{x}</label>)}</fieldset><fieldset><legend>MCP</legend>{['catalog-mcp','query-mcp','etl-mcp'].map(x=><label className="tool-check" key={x}><input type="checkbox" checked={draft.mcp_servers.includes(x)} onChange={()=>flip('mcp_servers',x)}/>{x}</label>)}</fieldset><fieldset><legend>Skills</legend>{['sql-analysis','metric-interpretation','data-quality'].map(x=><label className="tool-check" key={x}><input type="checkbox" checked={draft.skills.includes(x)} onChange={()=>flip('skills',x)}/>{x}</label>)}</fieldset><button className="save-agent">保存配置</button></form></div>:<div className="provider-console"><div className="provider-grid">{providers.map(p=><article className="provider-card" key={p.id}><div><b>{p.name}</b><em>{p.enabled?'已启用':'未启用'}</em></div><small>{p.kind}</small><code>{p.base_url}</code><span>Key {p.api_key_mask||'未设置'}</span></article>)}</div><div className="provider-forms"><form className="config-form" onSubmit={addProvider}><h2>添加供应商</h2><input name="name" required placeholder="名称，如 DeepSeek / 本地 vLLM"/><select name="kind"><option value="openai-compatible">OpenAI Compatible</option><option value="ollama">Ollama (Local)</option><option value="vllm">vLLM (Local)</option></select><input name="base_url" required placeholder="Base URL，如 http://localhost:11434/v1"/><input name="api_key" type="password" placeholder="API Key（本地模型可留空）"/><button>保存供应商</button></form><form className="config-form" onSubmit={addModel}><h2>添加并启用模型</h2><select name="provider_id" required><option value="">选择供应商</option>{providers.filter(p=>p.enabled).map(p=><option key={p.id} value={p.id}>{p.name}</option>)}</select><input name="model_id" required placeholder="模型 ID，如 deepseek-v4-flash"/><input name="display_name" required placeholder="显示名称"/><button>启用模型</button></form></div><div className="model-list">{models.map(m=><div key={m.id}><b>{m.display_name}</b><span>{m.enabled?'已启用':'未启用'} · {m.model_id}</span></div>)}</div></div>}</section>;
}

function App() {
  const [view, setView] = useState('new-chat');
  const [conversations, setConversations] = useState(() => [{ id: crypto.randomUUID(), title: '新对话', messages: [], updatedAt: Date.now() }]);
  const [activeId, setActiveId] = useState(null);
  const [recentOpen, setRecentOpen] = useState(true);
  const active = conversations.find(item => item.id === activeId) || conversations[0];
  useEffect(() => { if (!activeId) setActiveId(active.id); }, [activeId, active.id]);
  const createConversation = () => {
    const item = { id: crypto.randomUUID(), title: '新对话', messages: [], updatedAt: Date.now() };
    setConversations(items => [item, ...items]); setActiveId(item.id); setView('new-chat');
  };
  const updateMessages = updater => setConversations(items => items.map(item => item.id === active.id ? { ...item, messages: typeof updater === 'function' ? updater(item.messages) : updater, updatedAt: Date.now() } : item));
  const setTitle = text => setConversations(items => items.map(item => item.id === active.id ? { ...item, title: item.title === '新对话' ? text.slice(0, 18) : item.title, updatedAt: Date.now() } : item));
  const sorted = [...conversations].filter(item => item.messages.length).sort((a, b) => b.updatedAt - a.updatedAt);
  const content = useMemo(() => view === 'new-chat' ? <ChatHome session={active.id} messages={active.messages} setMessages={updateMessages} onFirstMessage={setTitle} onNewConversation={createConversation} /> : view === 'catalog' ? <Catalog /> : view === 'extensions' ? <Placeholder title="智能体扩展" text="后续在此添加用户自己的 Skills、MCP 服务和工具连接。" /> : <AgentManager />, [view, activeId, conversations]);
  return <div className="console-shell"><aside className="console-sidebar"><div className="console-brand">✦ Data Agent</div><nav>{nav.map(([id, icon, label]) => <button key={id} className={view === id ? 'active' : ''} onClick={() => id === 'new-chat' ? createConversation() : setView(id)}><span>{icon}</span>{label}</button>)}</nav><div className="recent-block"><button className="recent-toggle" onClick={() => setRecentOpen(open => !open)}>最近 <span>{recentOpen ? '⌃' : '⌄'}</span></button>{recentOpen && (sorted.length ? sorted.map(item => <button className={`recent-item ${active.id === item.id && view === 'new-chat' ? 'active' : ''}`} key={item.id} onClick={() => { setActiveId(item.id); setView('new-chat'); }}>{item.title}</button>) : <small>暂无最近对话</small>)}</div><div className="user-id"><span>●</span><div><b>local-user</b><small>用户 ID · u_001</small></div></div></aside><main>{content}</main></div>;
}
createRoot(document.getElementById('root')).render(<App />);
