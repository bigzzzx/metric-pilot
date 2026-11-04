import { useEffect, useState } from 'react';

import { decideApproval, getApproval } from '../../api/tools.js';

const terminalLabels = {
  approved: '已批准，正在恢复任务',
  rejected: '已拒绝',
  consumed: '审批已使用',
  expired: '已过期',
  cancelled: '已取消',
};

export default function ToolApprovalCard({ approval, onResolved }) {
  const approvalId = approval?.approval_id || approval?.id;
  const [status, setStatus] = useState(approval?.status || 'pending');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);

  useEffect(() => {
    if (!approvalId) return undefined;
    let active = true;
    getApproval(approvalId)
      .then(item => { if (active) { setStatus(item.status); setResult(item.result || null); } })
      .catch(() => {});
    return () => { active = false; };
  }, [approvalId]);

  const decide = async decision => {
    if (busy || status !== 'pending') return;
    setBusy(true); setError('');
    try {
      const payload = await decideApproval(approvalId, decision);
      setStatus(payload.approval?.status || decision);
      setResult(payload.tool_result || null);
      await onResolved?.(payload);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setBusy(false);
    }
  };

  const effects = approval?.effects || approval?.display?.estimated_effects || [];
  const summary = approval?.summary || approval?.display?.summary || '执行高风险工具操作';
  const target = approval?.target || approval?.display?.target || '';
  const resultMessage = result?.error?.message || result?.data?.message;
  const executionFailed = ['failed', 'unavailable', 'denied', 'rejected'].includes(result?.status);
  const statusLabel = status === 'consumed'
    ? executionFailed ? '已批准，执行未完成' : result?.status === 'success' ? '已批准并执行' : '审批已使用'
    : terminalLabels[status] || status;
  return <section className="tool-approval-card" aria-label="高风险工具审批">
    <div className="approval-heading"><b>{approval?.tool_name || '高风险操作'}</b><span>{status === 'pending' ? '需要批准' : '审批已处理'}</span></div>
    <p>{summary}</p>
    {approval?.requested_by && <small>申请人：{approval.requested_by}</small>}
    {target && <dl><dt>目标</dt><dd>{target}</dd></dl>}
    {effects.length > 0 && <ul>{effects.map((effect, index) => <li key={index}>{effect}</li>)}</ul>}
    {approval?.required_permission && <small>所需权限：<code>{approval.required_permission}</code></small>}
    {status === 'pending' ? <div className="approval-actions">
      <button type="button" disabled={busy} onClick={() => decide('approved')}>批准本次</button>
      <button type="button" className="outline-button" disabled={busy} onClick={() => decide('rejected')}>拒绝</button>
    </div> : <p className={`approval-state ${executionFailed ? 'rejected' : status}`}>{statusLabel}</p>}
    {resultMessage && <p className={`approval-result ${result?.status || ''}`}>{resultMessage}</p>}
    {error && <p className="approval-error">{error}</p>}
  </section>;
}

export function PermissionDeniedCard({ error }) {
  if (!error) return null;
  return <section className="permission-denied-card">
    <b>没有执行权限</b>
    <p>{error.message}</p>
    {error.required_permission && <small>需要申请：<code>{error.required_permission}</code></small>}
  </section>;
}
