import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

const samples = ['查询上月华东地区各品类销售额', 'orders 表中 amount 字段含义是什么？', '刷新昨日订单宽表', '删除旧订单表'];

function Trace({ data }) {
  return <details open><summary>执行轨迹 · {data.intent}</summary><div className="trace">{data.steps.map((step, index) => <div className="step" key={index}><b>{index + 1}. {step.name}</b><span>{step.detail}</span></div>)}</div></details>;
}
function Result({ data }) {
  if (!data.result?.length) return null;
  const columns = Object.keys(data.result[0]);
  return <table><thead><tr>{columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{data.result.map((row, index) => <tr key={index}>{columns.map((column) => <td key={column}>{row[column]}</td>)}</tr>)}</tbody></table>;
}
function App() {
  const [input, setInput] = useState(samples[0]);
  const [messages, setMessages] = useState([]);
  const [busy, setBusy] = useState(false);
  const send = async (message = input) => {
    if (!message.trim() || busy) return;
    setBusy(true); setMessages((items) => [...items, { role: 'user', text: message }]);
    try { const response = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message }) }); const data = await response.json(); setMessages((items) => [...items, { role: 'agent', data }]); setInput(''); }
    catch { setMessages((items) => [...items, { role: 'agent', data: { answer: '无法连接后端，请先运行 npm run api。', steps: [], intent: 'ERROR' } }]); }
    finally { setBusy(false); }
  };
  return <main><header><div><p className="eyebrow">LOCAL SIMULATION · AWS GLUE / ATHENA</p><h1>GenAI Data Agent</h1><p>自然语言管理数据目录、查询电商数据、触发受控 ETL。</p></div><span className="badge">离线模式</span></header>
    <section className="samples"><b>试试这些问题：</b>{samples.map((sample) => <button key={sample} onClick={() => send(sample)}>{sample}</button>)}</section>
    <section className="chat">{messages.length === 0 && <div className="empty">输入业务问题后，系统会展示 RAG 元数据命中、SQL 安全审核与工具执行过程。</div>}{messages.map((message, index) => message.role === 'user' ? <div className="user" key={index}>{message.text}</div> : <article key={index}><p>{message.data.answer}</p><Trace data={message.data}/>{message.data.sql && <details><summary>最终 SQL</summary><pre>{message.data.sql}</pre></details>}<Result data={message.data}/>{message.data.run_id && <Job runId={message.data.run_id}/>}</article>)}</section>
    <form onSubmit={(event) => { event.preventDefault(); send(); }}><input value={input} onChange={(event) => setInput(event.target.value)} placeholder="例如：查询上月华东地区各品类销售额" /><button disabled={busy}>{busy ? '执行中…' : '发送'}</button></form>
  </main>;
}
function Job({ runId }) { const [job, setJob] = useState({ status: 'RUNNING' }); React.useEffect(() => { const timer = setInterval(async () => { const data = await fetch(`/api/jobs/${runId}`).then((res) => res.json()); setJob(data); if (data.status !== 'RUNNING') clearInterval(timer); }, 700); return () => clearInterval(timer); }, [runId]); return <div className="job"><b>Glue Job（模拟）：{job.status}</b><small>{job.logs?.join(' · ')}</small></div>; }
createRoot(document.getElementById('root')).render(<App />);
