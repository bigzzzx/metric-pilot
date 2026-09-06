import assert from 'node:assert/strict';
import test from 'node:test';

import { shouldRefreshKnowledgeDocuments } from '../src/pages/extensions/knowledgeSelection.js';

test('an upload only refreshes documents for the still-selected knowledge base', () => {
  assert.equal(shouldRefreshKnowledgeDocuments('base-a', 'base-a'), true);
  assert.equal(shouldRefreshKnowledgeDocuments('base-a', 'base-b'), false);
  assert.equal(shouldRefreshKnowledgeDocuments('', 'base-b'), false);
});
