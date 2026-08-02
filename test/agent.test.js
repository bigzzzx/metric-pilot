import test from 'node:test';
import assert from 'node:assert/strict';
import { createDatabase } from '../server/db.js';
import { runAgent, validateSql, searchCatalog } from '../server/agent.js';

test('catalog finds orders metadata', () => assert.ok(searchCatalog('订单 amount 字段').some((item) => item.name === 'orders')));
test('guardrail rejects destructive SQL', () => assert.match(validateSql('DROP TABLE orders'), /仅允许/));
test('analytics query returns data', async () => { const answer = await runAgent('查询上月华东地区各品类销售额', createDatabase(), new Map()); assert.equal(answer.intent, 'DATA_QUERY'); assert.ok(answer.result.length > 0); });
test('unsafe user request is rejected', async () => assert.equal((await runAgent('删除旧订单表', createDatabase(), new Map())).intent, 'UNSAFE_REQUEST'));
test('ETL request creates a job', async () => { const jobs = new Map(); const answer = await runAgent('刷新昨日订单宽表', createDatabase(), jobs); assert.equal(answer.intent, 'ETL_TRIGGER'); assert.equal(jobs.get(answer.run_id).status, 'RUNNING'); });
test('missing key falls back to local rules', async () => { const answer = await runAgent('查询销售额', createDatabase(), new Map()); assert.equal(answer.modelMode, 'fallback'); });
test('model plan is parsed with mocked fetch', async () => {
  const previousKey = process.env.DEEPSEEK_API_KEY;
  const previousFetch = global.fetch;
  process.env.DEEPSEEK_API_KEY = 'test-key';
  global.fetch = async () => ({ ok: true, json: async () => ({ choices: [{ message: { content: JSON.stringify({ intent: 'CHAT', answer: '你好，我可以继续分析上一轮查询。', sql: '', should_analyze: false, reason: '普通问候' }) } }] }) });
  const answer = await runAgent('你好', createDatabase(), new Map());
  global.fetch = previousFetch;
  if (previousKey === undefined) delete process.env.DEEPSEEK_API_KEY; else process.env.DEEPSEEK_API_KEY = previousKey;
  assert.equal(answer.modelMode, 'deepseek');
  assert.equal(answer.intent, 'CHAT');
});
