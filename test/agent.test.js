import test from 'node:test';
import assert from 'node:assert/strict';
import { createDatabase } from '../server/db.js';
import { runAgent, validateSql, searchCatalog } from '../server/agent.js';

test('catalog finds orders metadata', () => assert.ok(searchCatalog('订单 amount 字段').some((item) => item.name === 'orders')));
test('guardrail rejects destructive SQL', () => assert.match(validateSql('DROP TABLE orders'), /仅允许/));
test('analytics query returns data', () => { const answer = runAgent('查询上月华东地区各品类销售额', createDatabase(), new Map()); assert.equal(answer.intent, 'ANALYTICS_QUERY'); assert.ok(answer.result.length > 0); });
test('unsafe user request is rejected', () => assert.equal(runAgent('删除旧订单表', createDatabase(), new Map()).intent, 'UNSAFE_REQUEST'));
test('ETL request creates a job', () => { const jobs = new Map(); const answer = runAgent('刷新昨日订单宽表', createDatabase(), jobs); assert.equal(answer.intent, 'ETL_TRIGGER'); assert.equal(jobs.get(answer.run_id).status, 'RUNNING'); });
test('invalid field is corrected once', () => { const answer = runAgent('查询销售额，修复错误字段', createDatabase(), new Map()); assert.match(answer.answer, /自动修复/); });
