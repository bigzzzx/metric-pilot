import { useEffect, useState } from 'react';

export default function VisionModelSettings() {
  const [models, setModels] = useState([]);
  const [error, setError] = useState('');
  useEffect(() => { fetch('/api/attachment-models').then(r => r.ok ? r.json() : Promise.reject(Error('图片模型配置加载失败'))).then(setModels).catch(e => setError(e.message)); }, []);
  const change = async (model, vision) => {
    const response = await fetch(`/api/attachment-models/${model.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ vision }) });
    if (!response.ok) { setError('保存失败，请检查管理员权限。'); return; }
    setModels(items => items.map(item => item.id === model.id ? { ...item, vision } : item)); setError('');
  };
  return <section className="vision-settings"><h2>图片理解</h2><p>请仅勾选模型供应商明确支持图片输入的模型。聊天上传图片后会使用这里启用的模型；文档仍使用原来的聊天模型。</p>
    {models.filter(model => model.kind !== 'anthropic-messages').map(model => <label key={model.id}><input type="checkbox" checked={Boolean(model.vision)} onChange={event => change(model, event.target.checked)} />{model.display_name}</label>)}
    {!models.length && <p>请先添加并启用模型。</p>}{error && <p role="alert">{error}</p>}
  </section>;
}
