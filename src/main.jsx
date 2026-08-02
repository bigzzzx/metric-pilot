import React, { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import catalog from '../metadata/catalog.json';
import './styles.css';

const samples = ['查询上月华东地区各品类销售额', 'orders 表中 amount 字段含义是什么？', '刷新昨日订单宽表', '删除旧订单表'];
const navItems = [
  { id: 'chat', icon: '▣', label: '对话工作台' },
  { id: 'catalog', icon: '▤', label: '数据目录' },
  { id: 'jobs', icon: '◷', label: '任务运行' },
  { id: 'history', icon: '⌕', label: '查询历史' },
];

function Trace({ data, compact = false }) {
  if (!data?.steps?.length) return null;
  return <div className={`trace ${compact ? 'trace-compact' : ''}`}>{data.steps.map((step, index) => <div className="trace-step" key={`${step.name}-${index}`}><span>{String(index + 1).padStart(2, '0')}</span><div><b>{step.name}</b><small>{step.detail}</small></div></div>)}</div>;
}

function QueryResult({ data }) {
  if (!data.result?.length) return null;
  const columns = Object.keys(data.result[0]);
  return <div className="table-wrap"><table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{data.result.map((row, index) => <tr key={index}>{columns.map((column) => <td key={column}>{row[column]}</td>)}</tr>)}</tbody></table></div>;
}

function JobStatus({ runId, onUpdate }) {
  const [job, setJob] = useState({ status: 'RUNNING', logs: [] });
  useEffect(() => {
    let active = true;
    const refresh = async () => {
      const data = await fetch(`/api/jobs/${runId}`).then((res) => res.json());
      if (!active) return;
      setJob(data); onUpdate?.(data);
    };
    refresh();
    const timer = setInterval(refresh, 700);
    return () => { active = false; clearInterval(timer); };
  }, [runId, onUpdate]);
  return <div className="job-status"><div><span className={`status-dot ${job.status === 'SUCCEEDED' ? 'success' : ''}`}></span><b>{job.status === 'SUCCEEDED' ? '任务已完成' : '任务运行中'}</b></div><small>{job.logs?.join(' · ') || '正在准备执行环境…'}</small></div>;
}

function Answer({ data, onJobUpdate }) {
  return <article className="answer"><div className="answer-label"><span>✦</span> Data Agent <em>{data.intent}</em></div><p>{data.answer}</p><div className="answer-tags">{data.catalog?.length > 0 && <span>Catalog RAG · {data.catalog.map((item) => item.name).join(', ')}</span>}{data.sql && <span className="approved">SQL 已审核</span>}</div>{data.sql && <details><summary>查看最终 SQL</summary><pre>{data.sql}</pre></details>}<QueryResult data={data}/>{data.run_id && <JobStatus runId={data.run_id} onUpdate={onJobUpdate}/>}<details className="mobile-trace"><summary>执行详情</summary><Trace data={data} compact /></details></article>;
}

function ChatWorkspace({ messages, setMessages, setLatest }) {
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const send = async (message) => {
    const text = (message ?? input).trim();
    if (!text || busy) return;
    setBusy(true); setMessages((items) => [...items, { role: 'user', text }]);
    try {
      const response = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: text }) });
      const data = await response.json();
      setMessages((items) => [...items, { role: 'agent', data }]); setLatest(data); setInput('');
    } catch {
      const data = { answer: '无法连接后端。请确认 API 服务正在运行。', steps: [], intent: 'ERROR' };
      setMessages((items) => [...items, { role: 'agent', data }]); setLatest(data);
    } finally { setBusy(false); }
  };
  return <section className="workspace chat-workspace"><div className="workspace-title"><div><h1>分析助手</h1><p>Ecommerce Lake · 当前会话</p></div><button className="more" aria-label="更多操作">⋯</button></div><div className="conversation">{messages.length === 0 && <div className="welcome"><h2>今天想了解哪些数据？</h2><p>可以查询业务指标、了解数据目录，或触发受控 ETL 任务。</p><div className="sample-grid">{samples.map((sample) => <button key={sample} onClick={() => send(sample)}>{sample}</button>)}</div></div>}{messages.map((message, index) => message.role === 'user' ? <div className="user-message" key={index}>{message.text}</div> : <Answer key={index} data={message.data} onJobUpdate={(job) => setLatest((current) => current?.run_id === job.run_id ? { ...current, job } : current)} />)}{busy && <div className="thinking"><i></i><i></i><i></i> 正在处理请求</div>}</div><form className="composer" onSubmit={(event) => { event.preventDefault(); send(); }}><input value={input} onChange={(event) => setInput(event.target.value)} placeholder="发送一条数据请求…" /><button aria-label="发送" disabled={busy}>↑</button></form><p className="composer-note">Agent 仅执行只读查询与受控任务。</p></section>;
}

function CatalogWorkspace() {
  const [selected, setSelected] = useState(catalog[0]);
  return <section className="workspace catalog-workspace"><div className="workspace-title"><div><h1>数据目录</h1><p>本地模拟 Glue Data Catalog · {catalog.length} 张表</p></div></div><div className="catalog-layout"><div className="table-list">{catalog.map((table) => <button key={table.name} className={selected.name === table.name ? 'active' : ''} onClick={() => setSelected(table)}><b>{table.name}</b><span>{table.description}</span></button>)}</div><div className="table-detail"><div className="detail-heading"><div><h2>{selected.name}</h2><p>{selected.description}</p></div><span className="schema-badge">TABLE</span></div><h3>字段</h3><div className="field-list">{selected.columns.map(([name, description]) => <div key={name}><code>{name}</code><span>{description}</span></div>)}</div>{selected.relations?.length > 0 && <><h3>关联关系</h3>{selected.relations.map((relation) => <div className="relation" key={relation}>{relation}</div>)}</>}</div></div></section>;
}

function JobsWorkspace({ latest }) {
  const job = latest?.job ?? (latest?.run_id ? { status: 'RUNNING', logs: [] } : null);
  return <section className="workspace"><div className="workspace-title"><div><h1>任务运行</h1><p>模拟 Glue Job 的状态与执行日志</p></div></div>{job ? <div className="job-card"><div><span className={`status-dot ${job.status === 'SUCCEEDED' ? 'success' : ''}`}></span><b>{latest?.run_id ?? 'refresh_order_wide'}</b><p>{job.status ?? 'RUNNING'}</p></div><div className="job-log">{(job.logs?.length ? job.logs : ['任务已创建，等待状态回传。']).map((line) => <p key={line}>› {line}</p>)}</div></div> : <div className="empty-state"><span>◷</span><h2>还没有任务运行</h2><p>在对话中输入“刷新昨日订单宽表”即可看到运行状态。</p></div>}</section>;
}

function HistoryWorkspace({ messages, setView }) {
  const questions = messages.filter((item) => item.role === 'user');
  return <section className="workspace"><div className="workspace-title"><div><h1>查询历史</h1><p>仅保存在当前浏览器会话中</p></div></div>{questions.length ? <div className="history-list">{questions.map((item, index) => <button key={`${item.text}-${index}`} onClick={() => setView('chat')}><span>⌕</span><div>{item.text}<small>本次会话 · 已执行</small></div><b>›</b></button>)}</div> : <div className="empty-state"><span>⌕</span><h2>暂无查询历史</h2><p>你的数据请求会在当前会话中显示在这里。</p></div>}</section>;
}

function ExecutionPanel({ latest }) {
  return <aside className="execution-panel"><div className="panel-title"><span>本次执行</span>{latest && <em>{latest.intent}</em>}</div>{latest ? <Trace data={latest} compact /> : <div className="execution-empty"><span>⌘</span><p>提交一个数据请求后，Agent 的检索、审核与执行步骤会显示在这里。</p></div>}</aside>;
}

function App() {
  const [view, setView] = useState('chat');
  const [messages, setMessages] = useState([]);
  const [latest, setLatest] = useState(null);
  const content = useMemo(() => {
    if (view === 'catalog') return <CatalogWorkspace />;
    if (view === 'jobs') return <JobsWorkspace latest={latest} />;
    if (view === 'history') return <HistoryWorkspace messages={messages} setView={setView} />;
    return <ChatWorkspace messages={messages} setMessages={setMessages} setLatest={setLatest} />;
  }, [view, messages, latest]);
  return <div className="app-shell"><aside className="sidebar"><div className="brand"><span>✦</span> Data Agent</div><nav>{navItems.map((item) => <button key={item.id} className={view === item.id ? 'active' : ''} onClick={() => setView(item.id)}><span>{item.icon}</span>{item.label}</button>)}</nav><div className="sidebar-status"><span></span><div>LOCAL MODE<small>服务运行正常</small></div></div></aside><main>{content}</main><ExecutionPanel latest={latest} /></div>;
}

createRoot(document.getElementById('root')).render(<App />);
