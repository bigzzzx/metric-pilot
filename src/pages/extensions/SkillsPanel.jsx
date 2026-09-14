import React, { useEffect, useMemo, useRef, useState } from 'react';
import './ExtensionManagement.css';

const emptySkill = {
  id: '', name: '', description: '', instructions: '', applicability: '',
  tags: [], examples: [], priority: 50, always_on: false, enabled: true, change_note: '',
};

async function jsonRequest(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || '请求失败');
  return payload;
}

export default function SkillsPanel({ canManage = false }) {
  const [skills, setSkills] = useState([]);
  const [skillId, setSkillId] = useState('');
  const [draft, setDraft] = useState(emptySkill);
  const [versions, setVersions] = useState([]);
  const [query, setQuery] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const versionRequest = useRef(0);

  const load = async preferred => {
    const values = await jsonRequest('/api/skills');
    if (!Array.isArray(values)) throw new Error('Skill 接口返回格式不正确');
    setSkills(values); setSkillId(current => {
      const candidate = preferred || current;
      return values.some(item => item.id === candidate) ? candidate : values[0]?.id || '';
    });
  };
  useEffect(() => { load().catch(error => setNotice(error.message)); }, []);
  useEffect(() => {
    const selected = skills.find(item => item.id === skillId);
    if (selected) {
      setDraft({ ...selected, change_note: '' });
      const request = ++versionRequest.current;
      jsonRequest(`/api/skills/${selected.id}/versions`).then(values => {
        if (request === versionRequest.current) setVersions(Array.isArray(values) ? values : []);
      }).catch(error => setNotice(error.message));
    } else if (!skillId) { setDraft(emptySkill); setVersions([]); }
  }, [skillId, skills]);

  const filtered = useMemo(() => {
    const key = query.trim().toLowerCase();
    return key ? skills.filter(skill => `${skill.name} ${skill.description} ${skill.tags.join(' ')}`.toLowerCase().includes(key)) : skills;
  }, [query, skills]);

  const save = async event => {
    event.preventDefault(); if (!canManage || busy) return; setBusy(true); setNotice('');
    try {
      const payload = {
        ...draft,
        tags: Array.isArray(draft.tags) ? draft.tags : String(draft.tags).split(',').map(value => value.trim()).filter(Boolean),
        examples: Array.isArray(draft.examples) ? draft.examples : String(draft.examples).split('\n').map(value => value.trim()).filter(Boolean),
      };
      const saved = await jsonRequest(draft.id ? `/api/skills/${draft.id}` : '/api/skills', {
        method: draft.id ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      setNotice(`Skill「${saved.name}」已保存为 v${saved.current_version}。`); await load(saved.id);
    } catch (error) { setNotice(`保存失败：${error.message}`); } finally { setBusy(false); }
  };

  const archive = async () => {
    if (!draft.id || !canManage || busy) return;
    setBusy(true); setNotice('');
    try {
      const payload = {
        ...draft,
        tags: Array.isArray(draft.tags) ? draft.tags : String(draft.tags).split(',').map(value => value.trim()).filter(Boolean),
        examples: Array.isArray(draft.examples) ? draft.examples : String(draft.examples).split('\n').map(value => value.trim()).filter(Boolean),
        archived: true, enabled: false, change_note: '归档',
      };
      await jsonRequest(`/api/skills/${draft.id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      setNotice(`Skill「${draft.name}」已归档。`); setSkillId(''); await load();
    } catch (error) { setNotice(`归档失败：${error.message}`); } finally { setBusy(false); }
  };

  const remove = async () => {
    if (!draft.id || !canManage || busy) return;
    if (!window.confirm(`确定永久删除 Skill“${draft.name}”吗？全部版本历史和 Agent 绑定也会一并删除。此操作不可恢复。`)) return;
    setBusy(true); setNotice('');
    try {
      const deleted = await jsonRequest(`/api/skills/${draft.id}`, { method: 'DELETE' });
      setSkillId(''); setVersions([]);
      await load();
      setNotice(`Skill“${deleted.name || draft.name}”已永久删除。`);
    } catch (error) { setNotice(`删除失败：${error.message}`); } finally { setBusy(false); }
  };

  return <div className="extension-page extension-management">
    <div className="extension-title-row"><div><h2>Skills</h2><p className="page-subtitle">管理可复用的业务行为规范；运行时只注入本轮选中的 Skill。</p></div>{canManage && <button className="outline-button" onClick={() => { setSkillId(''); setDraft(emptySkill); setVersions([]); }}>＋ 新建 Skill</button>}</div>
    {!canManage && <p className="page-subtitle">当前账号仅可查看 Skills；请联系平台管理员申请管理权限。</p>}
    {notice && <p className="save-notice catalog-notice" role="status">{notice}</p>}
    <div className="extension-console">
      <aside className="extension-nav"><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索 Skill" />{filtered.map(skill => <button key={skill.id} className={skill.id === skillId ? 'active' : ''} onClick={() => setSkillId(skill.id)}><b>{skill.name}</b><span>v{skill.current_version} · {skill.enabled ? '已启用' : '已停用'}</span><small>{skill.description || '无说明'}</small></button>)}{!filtered.length && <p>还没有匹配的 Skill。</p>}</aside>
      <main className="extension-detail">
        <form className="extension-editor" onSubmit={save}>
          <div className="editor-heading"><div><h3>{draft.id ? draft.name : '新建 Skill'}</h3><p>Skill 不授予工具权限，也不能覆盖审批规则。</p></div><label className="switch-label"><input type="checkbox" disabled={!canManage || busy} checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />启用</label></div>
          <fieldset disabled={!canManage || busy} className="extension-form-grid">
            <label>名称<input required value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} placeholder="例如：指标解释规范" /></label>
            <label>优先级<input type="number" min="0" max="100" value={draft.priority} onChange={event => setDraft({ ...draft, priority: Number(event.target.value) })} /></label>
            <label className="wide">简短说明<input value={draft.description} onChange={event => setDraft({ ...draft, description: event.target.value })} placeholder="这个 Skill 解决什么问题" /></label>
            <label className="wide">适用场景<textarea rows="3" value={draft.applicability} onChange={event => setDraft({ ...draft, applicability: event.target.value })} placeholder="例如：用户询问 GMV、订单量、退款口径或指标同比时" /></label>
            <label className="wide">执行规则<textarea required rows="10" value={draft.instructions} onChange={event => setDraft({ ...draft, instructions: event.target.value })} placeholder="写清楚必须遵循的业务规则、回答结构和限制" /></label>
            <label>标签<input value={Array.isArray(draft.tags) ? draft.tags.join(', ') : draft.tags} onChange={event => setDraft({ ...draft, tags: event.target.value })} placeholder="指标, GMV, 口径" /></label>
            <label>版本说明<input value={draft.change_note || ''} onChange={event => setDraft({ ...draft, change_note: event.target.value })} placeholder="本次修改内容" /></label>
            <label className="wide">示例（每行一个）<textarea rows="4" value={Array.isArray(draft.examples) ? draft.examples.join('\n') : draft.examples} onChange={event => setDraft({ ...draft, examples: event.target.value })} placeholder={'回答 GMV 时说明退款处理方式\n数据不足时明确说明限制'} /></label>
          </fieldset>
          <label className="tool-check"><input type="checkbox" disabled={!canManage || busy} checked={draft.always_on} onChange={event => setDraft({ ...draft, always_on: event.target.checked })} />绑定 Agent 后始终注入（普通 Skill 由路由按需选择）</label>
          <div className="extension-actions">{canManage && <button disabled={busy}>保存并创建新版本</button>}{draft.id && canManage && <><button type="button" className="outline-button" disabled={busy} onClick={archive}>归档</button><button type="button" className="danger-button" disabled={busy} onClick={remove}>永久删除</button></>}</div>
        </form>
        {draft.id && <section className="skill-version-list"><h3>版本历史</h3>{versions.map(version => <details key={version.id}><summary>v{version.version} · {version.change_note || '更新'}</summary><p>{version.created_at}</p><pre>{version.instructions}</pre></details>)}</section>}
      </main>
    </div>
  </div>;
}
