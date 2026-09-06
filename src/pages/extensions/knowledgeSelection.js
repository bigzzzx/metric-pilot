export function shouldRefreshKnowledgeDocuments(sourceBaseId, currentBaseId) {
  return Boolean(sourceBaseId) && sourceBaseId === currentBaseId;
}
