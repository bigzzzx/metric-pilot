import fs from 'node:fs';
import path from 'node:path';

const catalog = JSON.parse(fs.readFileSync(path.resolve('metadata/catalog.json'), 'utf8'));
const forbidden = /\b(drop|delete|insert|update|alter|create|truncate|attach|pragma)\b|;/i;
const allowedTables = new Set(catalog.map((table) => table.name));

export function searchCatalog(query) {
  const terms = query.toLowerCase().match(/[\u4e00-\u9fff]+|[a-z_]+/g) ?? [];
  return catalog.map((table) => {
    const text = `${table.name} ${table.description} ${table.columns.flat().join(' ')} ${(table.relations ?? []).join(' ')}`.toLowerCase();
    return { ...table, score: terms.reduce((score, term) => score + (text.includes(term) ? 1 : 0), 0) };
  }).filter((table) => table.score > 0).sort((a, b) => b.score - a.score).slice(0, 3);
}

export function validateSql(sql) {
  const normalized = sql.trim().replace(/\s+/g, ' ');
  if (!/^(select|with)\b/i.test(normalized) || forbidden.test(normalized)) return '仅允许单条只读 SELECT/CTE 查询。';
  const matches = [...normalized.matchAll(/\b(?:from|join)\s+([a-z_]+)/gi)].map((item) => item[1].toLowerCase());
  if (matches.some((table) => !allowedTables.has(table))) return 'SQL 使用了未登记的数据表。';
  return null;
}

function queryPlan(message) {
  const lower = message.toLowerCase();
  if (/(删|删除|drop|清空|修改|更新)/.test(lower)) return { intent: 'UNSAFE_REQUEST', answer: '为防止意外删表或写入数据，本演示 Agent 仅有只读查询与受控 ETL 权限；该请求未执行。' };
  if (/(刷新|etl|任务|job|宽表)/.test(lower)) return { intent: 'ETL_TRIGGER', job: 'refresh_order_wide' };
  if (/(字段|字段含义|表结构|元数据|catalog)/.test(lower) && !/(上月|华东|品类|销售额|销售|订单|用户|查询|多少|统计)/.test(lower)) return { intent: 'METADATA_QA' };
  if (/(上月|华东|品类|销售额|销售|订单|用户|查询|多少|统计)/.test(lower)) {
    const broken = /(错误字段|修复|修正)/.test(lower);
    return { intent: 'ANALYTICS_QUERY', sql: broken
      ? 'SELECT category, SUM(total_amount) AS sales FROM orders GROUP BY category'
      : `SELECT p.category AS category, ROUND(SUM(o.amount), 2) AS sales_amount
         FROM orders o JOIN users u ON o.user_id = u.user_id JOIN products p ON o.product_id = p.product_id
         WHERE u.region = '华东' AND o.order_date >= '2026-07-01' AND o.order_date < '2026-08-01' AND o.status = 'PAID'
         GROUP BY p.category ORDER BY sales_amount DESC` };
  }
  return { intent: 'METADATA_QA' };
}

export function runAgent(message, db, jobs) {
  const steps = [{ name: '意图识别', detail: '确定性离线规划器正在解析业务意图。' }];
  const plan = queryPlan(message);
  const matches = searchCatalog(message);
  steps.push({ name: '目录检索', detail: `命中：${matches.map((item) => item.name).join('、') || '无直接命中'}` });
  if (plan.intent === 'UNSAFE_REQUEST') return { answer: plan.answer, intent: plan.intent, catalog: matches, steps: [...steps, { name: '安全拒绝', detail: '未调用任何执行工具。' }] };
  if (plan.intent === 'ETL_TRIGGER') {
    const runId = `job-${Date.now()}`;
    jobs.set(runId, { run_id: runId, job_name: plan.job, status: 'RUNNING', logs: ['参数校验通过', '读取 orders / users / products'], started_at: new Date().toISOString() });
    setTimeout(() => { const job = jobs.get(runId); if (job) { job.status = 'SUCCEEDED'; job.logs.push('生成 order_wide 模拟宽表', '任务完成'); } }, 1800);
    return { answer: '已启动订单宽表刷新任务。', intent: plan.intent, catalog: matches, run_id: runId, steps: [...steps, { name: '参数校验', detail: 'job=refresh_order_wide，run_date=昨日（模拟）。' }, { name: 'Glue Job', detail: `已创建运行 ${runId}` }] };
  }
  if (plan.intent === 'METADATA_QA') {
    const target = matches[0] ?? catalog.find((item) => item.name === 'orders');
    const amount = /amount/.test(message) ? '其中 amount 表示订单实付金额，单位为人民币元。' : '';
    return { answer: `${target.name}：${target.description}${amount}`, intent: plan.intent, catalog: [target], steps: [...steps, { name: '元数据回答', detail: '基于受控目录上下文生成回答。' }] };
  }
  steps.push({ name: 'SQL 生成', detail: plan.sql });
  const rejected = validateSql(plan.sql);
  if (rejected) return { answer: rejected, intent: plan.intent, catalog: matches, sql: plan.sql, steps: [...steps, { name: 'SQL 安全审核', detail: rejected }] };
  try {
    const rows = db.prepare(plan.sql).all();
    return { answer: `查询完成，共返回 ${rows.length} 行。`, intent: plan.intent, catalog: matches, sql: plan.sql, result: rows, steps: [...steps, { name: 'SQL 安全审核', detail: '通过：单条只读查询。' }, { name: 'Athena 查询（本地 SQLite）', detail: `成功返回 ${rows.length} 行。` }] };
  } catch (error) {
    const repairedSql = `SELECT p.category AS category, ROUND(SUM(o.amount), 2) AS sales_amount FROM orders o JOIN products p ON o.product_id = p.product_id GROUP BY p.category ORDER BY sales_amount DESC`;
    const rows = db.prepare(repairedSql).all();
    return { answer: '首次 SQL 因字段不存在失败，已基于目录上下文自动修复并执行成功。', intent: plan.intent, catalog: matches, sql: repairedSql, result: rows, steps: [...steps, { name: '执行失败', detail: String(error.message).replace(/\n/g, ' ') }, { name: '一次自动修复', detail: repairedSql }, { name: 'Athena 查询（本地 SQLite）', detail: `修复后返回 ${rows.length} 行。` }] };
  }
}
