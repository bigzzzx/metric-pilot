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

function Composer({ models, modelId, setModelId, agents, agentId, setAgentId, onSend, busy }) {
  const [value, setValue] = useState('');
  const submit = event => { event.preventDefault(); onSend(value); setValue(''); };
  return <form className="home-composer" onSubmit={submit}>
    <textarea value={value} onChange={event => setValue(event.target.value)} placeholder="发消息给 Data Agent…" rows="3" />
    <div className="composer-toolbar">
      <span className="composer-add">＋</span>
      <select aria-label="选择智能体" value={agentId} onChange={e => setAgentId(e.target.value)}>
        {agents.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
      </select>
      <span className="toolbar-spacer" />
      <select aria-label="选择模型" value={modelId} onChange={e => setModelId(e.target.value)}>
        <option value="">默认模型</option>
        {models.map(model => <option key={model.id} value={model.id}>{model.display_name}</option>)}
      </select>
      <button type="submit" disabled={busy || !value.trim()} aria-label="发送">↑</button>
    </div>
  </form>;
}

function ChatHome({ session, setSession, addRecent }) {
  const [messages, setMessages] = useState([]);
  const [models, setModels] = useState([]);
  const [agents, setAgents] = useState([]);
  const [modelId, setModelId] = useState('');
  const [agentId, setAgentId] = useState('data-agent');
  const [busy, setBusy] = useState(false);
  useEffect(() => { Promise.all([fetch('/api/models').then(r => r.json()), fetch('/api/agents').then(r => r.json())]).then(([modelData, agentData]) => { setModels(modelData); setAgents(agentData); setModelId(modelData[0]?.id || ''); setAgentId(agentData[0]?.id || 'data-agent'); }); }, []);
  const send = async text => {
    const message = text.trim(); if (!message || busy) return;
    setBusy(true); setMessages(items => [...items, { role: 'user', text: message }]); addRecent(message);
    try {
      const response = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message, session_id: session, model_id: modelId || null, agent_id: agentId }) });
      const raw = await response.text(); const data = JSON.parse(raw);
      if (!response.ok) throw Error(data.detail || '请求失败');
      setMessages(items => [...items, { role: 'assistant', data }]);
    } catch (error) { setMessages(items => [...items, { role: 'assistant', data: { intent: 'ERROR', answer: error.message } }]); }
    finally { setBusy(false); }
  };
  const newConversation = () => { setMessages([]); setSession(crypto.randomUUID()); };
  return <section className="chat-page">
    {messages.length === 0 ? <div className="home-welcome"><h1>你好，有什么可以帮助你的？</h1><Composer {...{ models, modelId, setModelId, agents, agentId, setAgentId, onSend: send, busy }} /></div> : <><div className="conversation-list">{messages.map((item, index) => item.role === 'user' ? <div className="user-bubble" key={index}>{item.text}</div> : <AssistantAnswer key={index} data={item.data} />)}</div><div className="chat-docked"><Composer {...{ models, modelId, setModelId, agents, agentId, setAgentId, onSend: send, busy }} /></div><button className="new-thread-small" onClick={newConversation}>新对话</button></>}
  </section>;
}

function Catalog() { const [chosen, setChosen] = useState(catalog[0]); return <section className="page"><h1>数据目录</h1><p className="page-subtitle">查看已登记的数据资产、字段与关联关系。</p><div className="catalog-layout"><div className="table-list">{catalog.map(item => <button key={item.name} className={chosen.name === item.name ? 'active' : ''} onClick={() => setChosen(item)}><b>{item.name}</b><span>{item.description}</span></button>)}</div><div className="table-detail"><h2>{chosen.name}</h2><p>{chosen.description}</p><div className="field-list">{chosen.columns.map(([name, description]) => <div key={name}><code>{name}</code><span>{description}</span></div>)}</div></div></div></section>; }
function Placeholder({ title, text }) { return <section className="page placeholder-page"><h1>{title}</h1><p>{text}</p></section>; }

function App() {
  const [view, setView] = useState('new-chat');
  const [session, setSession] = useState(() => crypto.randomUUID());
  const [recent, setRecent] = useState([]);
  const addRecent = text => setRecent(items => [{ id: crypto.randomUUID(), text, time: new Date() }, ...items.filter(item => item.text !== text)].slice(0, 6));
  const content = useMemo(() => view === 'new-chat' ? <ChatHome session={session} setSession={setSession} addRecent={addRecent} /> : view === 'catalog' ? <Catalog /> : view === 'extensions' ? <Placeholder title="智能体扩展" text="后续在此添加用户自己的 Skills、MCP 服务和工具连接。" /> : <Placeholder title="智能体管理" text="后续在此管理 Agent、模型供应商和模型配置。" />, [view, session, recent]);
  return <div className="console-shell"><aside className="console-sidebar"><div className="console-brand">✦ Data Agent</div><nav>{nav.map(([id, icon, label]) => <button key={id} className={view === id ? 'active' : ''} onClick={() => { setView(id); if (id === 'new-chat') setSession(crypto.randomUUID()); }}><span>{icon}</span>{label}</button>)}</nav><div className="recent-block"><div>最近 <span>⌄</span></div>{recent.length ? recent.map(item => <button key={item.id} onClick={() => setView('new-chat')}>{item.text}</button>) : <small>暂无最近对话</small>}</div><div className="user-id"><span>●</span><div><b>local-user</b><small>用户 ID · u_001</small></div></div></aside><main>{content}</main></div>;
}
createRoot(document.getElementById('root')).render(<App />);
