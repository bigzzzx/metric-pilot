export async function serviceRequest(path, options = {}) {
  const response = await fetch(`/api/data-services${path}`, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...options.headers },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === 'string' ? detail : detail?.message || (Array.isArray(detail) ? detail.map(item => item.msg).join('；') : '请求失败，请重试。'));
  }
  return data;
}

export function serviceQueryBody(metric, start, end, dimensions, filters = []) {
  return { metric_id: metric.metric_id, version: metric.version, start_date: start || null,
    end_date: end || null, dimensions, filters };
}
