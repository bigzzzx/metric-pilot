async function jsonRequest(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail;
    throw new Error(detail?.message || detail || `请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

export function getApproval(approvalId) {
  return jsonRequest(`/api/tool-approvals/${approvalId}`);
}

export function decideApproval(approvalId, decision) {
  return jsonRequest(`/api/tool-approvals/${approvalId}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision }),
  });
}
