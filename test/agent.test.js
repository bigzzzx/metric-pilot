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
  global.fetch = async () => ({ ok: true, json: async () => ({ choices: [{ message: { content: `模型计划：\n\`\`\`json\n${JSON.stringify({ intent: 'CHAT', answer: '你好，我可以继续分析上一轮查询。', sql: '', should_analyze: false, reason: '普通问候' })}\n\`\`\`` } }] }) });
  const answer = await runAgent('你好', createDatabase(), new Map());
  global.fetch = previousFetch;
  if (previousKey === undefined) delete process.env.DEEPSEEK_API_KEY; else process.env.DEEPSEEK_API_KEY = previousKey;
  assert.equal(answer.modelMode, 'deepseek');
  assert.equal(answer.intent, 'CHAT');
});

test('model receives prior conversation as chat messages', async () => {
  const previousKey = process.env.DEEPSEEK_API_KEY;
  const previousFetch = global.fetch;
  let requestBody;
  process.env.DEEPSEEK_API_KEY = 'test-key';
  global.fetch = async (_url, options) => {
    requestBody = JSON.parse(options.body);
    return { ok: true, json: async () => ({ choices: [{ message: { content: JSON.stringify({ intent: 'CHAT', answer: '我记得上一轮的销售额结果。', sql: '', should_analyze: false, reason: '连续追问' }) } }] }) };
  };
  await runAgent('那为什么数码最高？', createDatabase(), new Map(), { history: [{ question: '查询销售额', answer: '数码销售额最高。' }] });
  global.fetch = previousFetch;
  if (previousKey === undefined) delete process.env.DEEPSEEK_API_KEY; else process.env.DEEPSEEK_API_KEY = previousKey;
  assert.deepEqual(requestBody.messages.slice(1, 3), [{ role: 'user', content: '查询销售额' }, { role: 'assistant', content: '数码销售额最高。' }]);
});

test('a trailing SQL semicolon is normalized before safety validation', async () => {
  const previousKey = process.env.DEEPSEEK_API_KEY;
  const previousFetch = global.fetch;
  process.env.DEEPSEEK_API_KEY = 'test-key';
  global.fetch = async () => ({ ok: true, json: async () => ({ choices: [{ message: { content: JSON.stringify({ intent: 'DATA_QUERY', answer: '正在查询。', sql: 'SELECT category FROM products;', should_analyze: false, reason: '查询商品品类' }) } }] }) });
  const answer = await runAgent('有哪些商品品类？', createDatabase(), new Map());
  global.fetch = previousFetch;
  if (previousKey === undefined) delete process.env.DEEPSEEK_API_KEY; else process.env.DEEPSEEK_API_KEY = previousKey;
  assert.equal(answer.modelMode, 'deepseek');
  assert.equal(answer.sql.includes(';'), false);
  assert.ok(answer.result.length > 0);
});
