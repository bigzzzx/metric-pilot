const JSON_HEADERS = { 'Content-Type': 'application/json' };

export class KnowledgeApiError extends Error {
  constructor(message, { status = 0, code = '', requestId = '', warnings = [] } = {}) {
    super(message);
    this.name = 'KnowledgeApiError';
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.warnings = Array.isArray(warnings) ? warnings : [];
  }
}

async function requestJson(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { credentials: 'same-origin', ...options });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    throw new KnowledgeApiError('无法连接知识库服务，请确认后端已启动后重试。', {
      code: 'network_error',
    });
  }

  const raw = await response.text();
  let payload = {};
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch {
      if (response.ok) {
        throw new KnowledgeApiError('知识库服务返回了无法识别的数据。', {
          status: response.status,
          code: 'invalid_response',
        });
      }
    }
  }

  if (!response.ok) {
    const detail = payload.detail;
    const detailMessage = typeof detail === 'string' ? detail : detail?.message;
    throw new KnowledgeApiError(
      detailMessage || payload.message || `知识库请求失败（HTTP ${response.status}）。`,
      {
        status: response.status,
        code: payload.error_code || payload.code || detail?.code || 'request_failed',
        requestId: payload.request_id || detail?.request_id || '',
        warnings: payload.warnings || detail?.warnings || [],
      },
    );
  }

  return payload;
}

export function listKnowledgeBases(options = {}) {
  return requestJson('/api/knowledge-bases', options);
}

export function createKnowledgeBase({ name, description }, options = {}) {
  return requestJson('/api/knowledge-bases', {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ name, description }),
    ...options,
  });
}

export function listKnowledgeDocuments(knowledgeBaseId, options = {}) {
  return requestJson(`/api/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents`, options);
}

export function importKnowledgeText(knowledgeBaseId, { name, content, sourceType = 'text' }, options = {}) {
  return requestJson(`/api/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents`, {
    method: 'POST',
    headers: JSON_HEADERS,
    body: JSON.stringify({ name, content, source_type: sourceType }),
    ...options,
  });
}

export function uploadKnowledgeFile(knowledgeBaseId, file, options = {}) {
  const body = new FormData();
  body.append('file', file, file.name);
  return requestJson(`/api/knowledge-bases/${encodeURIComponent(knowledgeBaseId)}/documents/upload`, {
    method: 'POST',
    body,
    ...options,
  });
}

export function normalizeImportResult(payload = {}) {
  const document = payload.document && typeof payload.document === 'object' ? payload.document : payload;
  const warnings = payload.warnings || document.warnings || [];
  return {
    documentId: payload.document_id || document.id || '',
    status: payload.status || document.status || 'ready',
    parser: payload.parser || document.parser || '',
    chunkCount: Number(payload.chunk_count ?? document.chunk_count ?? 0),
    warnings: Array.isArray(warnings) ? warnings : [String(warnings)],
    errorCode: payload.error_code || document.error_code || '',
    message: payload.message || document.message || '',
  };
}

export function presentKnowledgeError(error) {
  if (!(error instanceof KnowledgeApiError)) return error?.message || '发生未知错误，请稍后重试。';
  const suffix = [error.code && `错误码：${error.code}`, error.requestId && `请求 ID：${error.requestId}`]
    .filter(Boolean)
    .join('；');
  return suffix ? `${error.message}（${suffix}）` : error.message;
}
