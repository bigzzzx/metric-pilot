/*
 * Real-browser acceptance for the extension management surface.
 *
 * It deliberately performs every product mutation through the React UI:
 * create a knowledge base and text document, create a Skill, create/sync/test
 * an MCP server, bind the extensions to Agents, and send three real chats.
 *
 * A short-lived local admin session is supplied by EXTENSION_UI_TOKEN. The
 * wrapper that invokes this script creates and revokes that token; this script
 * never prints it or writes it to the report.
 */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const baseURL = process.env.EXTENSION_UI_BASE_URL || 'http://127.0.0.1:5174';
const token = process.env.EXTENSION_UI_TOKEN;
const output = path.resolve('.data', 'extension-frontend-acceptance.json');
const suffix = '20260914';
const names = {
  knowledge: `前端验收知识库-${suffix}`,
  skill: `前端验收回答格式-${suffix}`,
  mcp: `前端验收指标 MCP-${suffix}`,
};
const report = {
  started_at: new Date().toISOString(),
  base_url: baseURL,
  resources: names,
  checks: [],
  page_errors: [],
};

if (!token) throw new Error('EXTENSION_UI_TOKEN is required');

function parseSse(raw) {
  return raw.split(/\r?\n\r?\n/).map(block => ({
    event: block.match(/^event:\s*(.+)$/m)?.[1]?.trim() || 'message',
    text: block.match(/^data:\s*(.+)$/m)?.[1],
  })).filter(item => item.text).map(item => ({ ...item, data: JSON.parse(item.text) }));
}

async function main() {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1100 } });
  await context.addCookies([{
    name: 'data_agent_session', value: token, domain: '127.0.0.1', path: '/',
    httpOnly: true, sameSite: 'Lax',
  }]);
  const page = await context.newPage();
  page.on('pageerror', error => report.page_errors.push(error.message));
  const check = async (name, action) => {
    const started = Date.now();
    try {
      const details = await action();
      report.checks.push({ name, passed: true, duration_ms: Date.now() - started, details });
      console.log(`PASS ${name}`);
      return details;
    } catch (error) {
      report.checks.push({ name, passed: false, duration_ms: Date.now() - started, error: error.message });
      await page.screenshot({ path: path.resolve('.data', `extension-frontend-failure-${report.checks.length}.png`), fullPage: true }).catch(() => {});
      console.error(`FAIL ${name}: ${error.message}`);
      throw error;
    }
  };
  const navigateExtensions = async tab => {
    await page.getByRole('button', { name: '智能体扩展', exact: false }).click();
    await page.getByRole('tab', { name: tab, exact: true }).click();
  };
  const send = async message => {
    const responsePromise = page.waitForResponse(response => (
      response.url().includes('/api/chat/stream') && response.request().method() === 'POST'
    ), { timeout: 140000 });
    await page.getByPlaceholder('发消息给 Data Agent…').fill(message);
    await page.getByRole('button', { name: '发送', exact: true }).click();
    const response = await responsePromise;
    const raw = await response.text();
    assert.equal(response.status(), 200, raw);
    const events = parseSse(raw);
    const error = events.find(item => item.event === 'error');
    assert.ok(!error, JSON.stringify(error?.data));
    const done = events.find(item => item.event === 'done');
    assert.ok(done, `chat did not return a terminal event: ${raw.slice(-500)}`);
    await page.waitForFunction(() => !document.querySelector('.stream-status'), undefined, { timeout: 30000 }).catch(() => {});
    return { ...done.data, events: events.map(item => item.event) };
  };
  const newConversation = async () => {
    const created = page.waitForResponse(response => (
      response.url().includes('/api/conversations') && response.request().method() === 'POST'
    ));
    await page.getByRole('button', { name: '创建新对话', exact: false }).click();
    await (await created).finished();
    await page.locator('.chat-page .home-welcome').waitFor({ timeout: 15000 });
  };
  const chooseAgent = async id => {
    const agentsResponse = await context.request.get('/api/agents');
    assert.equal(agentsResponse.status(), 200, await agentsResponse.text());
    const agents = await agentsResponse.json();
    const agent = agents.find(item => item.id === id);
    assert.ok(agent, `Agent not found: ${id}`);
    await page.getByRole('button', { name: '智能体管理', exact: false }).click();
    await page.locator('.agent-list').getByRole('button', { name: new RegExp(agent.name) }).click();
    await page.locator('.agent-editor').waitFor();
    return agent;
  };
  const saveAgent = async () => {
    const request = page.waitForResponse(response => (
      /\/api\/agents\/[^/]+$/.test(new URL(response.url()).pathname) && response.request().method() === 'PUT'
    ));
    await page.getByRole('button', { name: '保存配置', exact: true }).click();
    const response = await request;
    assert.equal(response.status(), 200, await response.text());
    await page.getByText('智能体配置已保存。', { exact: true }).waitFor();
  };

  try {
    await check('administrator_browser_session', async () => {
      await page.goto(baseURL);
      await page.locator('.console-sidebar').waitFor({ timeout: 20000 });
      await page.getByPlaceholder('发消息给 Data Agent…').waitFor();
      assert.equal(await page.getByRole('button', { name: '系统管理', exact: false }).count(), 1);
      return { logged_in: true, account: 'user1' };
    });

    await check('knowledge_base_create_ingest_and_chat_use', async () => {
      await navigateExtensions('知识库');
      await page.getByRole('button', { name: '新建知识库', exact: false }).click();
      const create = page.waitForResponse(response => (
        new URL(response.url()).pathname === '/api/knowledge-bases' && response.request().method() === 'POST'
      ));
      await page.locator('.new-kb-form input[name="name"]').fill(names.knowledge);
      await page.locator('.new-kb-form input[name="description"]').fill('浏览器验收创建；验证文本导入、切片、向量索引与 RAG 使用。');
      await page.locator('.new-kb-form').getByRole('button', { name: '创建', exact: true }).click();
      assert.equal((await create).status(), 201);
      await page.getByText('知识库已创建。', { exact: true }).waitFor();
      await page.locator('.knowledge-import input[name="name"]').fill(`KB-EXT-${suffix} 验收规则`);
      await page.locator('.knowledge-import textarea[name="content"]').fill(
        `验收规则 KB-EXT-${suffix}：唯一校验短语是 MANGO-GRAPH-71。GMV 只统计 status=PAID 的订单金额；退款和取消订单均不计入 GMV。`
      );
      const ingest = page.waitForResponse(response => (
        /\/api\/knowledge-bases\/[^/]+\/documents$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST'
      ), { timeout: 120000 });
      await page.getByRole('button', { name: '解析、切片并写入知识库', exact: true }).click();
      const ingestResponse = await ingest;
      assert.equal(ingestResponse.status(), 200, await ingestResponse.text());
      await page.getByText(/已完成.*切片|文档处理结果已更新/).waitFor({ timeout: 120000 });
      await newConversation();
      const answer = await send(`根据知识库 KB-EXT-${suffix} 回答：唯一校验短语是什么？GMV 是否包括退款订单？`);
      assert.match(answer.answer || '', /MANGO-GRAPH-71/);
      assert.match(answer.answer || '', /不计入|不包括|排除/);
      await page.screenshot({ path: path.resolve('.data', 'extension-knowledge-chat.png'), fullPage: true });
      return { document_indexed: true, answer: answer.answer, trace_steps: answer.trace?.steps?.map(step => step.node) || [] };
    });

    await check('skill_create_bind_and_chat_use', async () => {
      await navigateExtensions('Skills');
      await page.getByRole('button', { name: '新建 Skill', exact: false }).click();
      await page.getByLabel('名称', { exact: true }).fill(names.skill);
      await page.getByLabel('优先级', { exact: true }).fill('95');
      await page.getByLabel('简短说明', { exact: true }).fill('浏览器验收：验证绑定后的回答格式。');
      await page.getByLabel('适用场景', { exact: true }).fill(`用户消息包含 EXT-SKILL-${suffix} 时。`);
      await page.getByLabel('执行规则', { exact: true }).fill(
        `当用户消息包含 EXT-SKILL-${suffix} 时，回答必须以“【Skill 验收已生效】”开头，第二行以“结论：”开头。不得虚构数据，也不得调用写入工具。`
      );
      await page.getByLabel('标签', { exact: true }).fill('验收,格式');
      await page.getByLabel('版本说明', { exact: true }).fill('前端验收首次创建');
      await page.getByLabel('示例（每行一个）', { exact: true }).fill(`EXT-SKILL-${suffix}：解释数据血缘`);
      await page.getByLabel('绑定 Agent 后始终注入（普通 Skill 由路由按需选择）', { exact: true }).check();
      const saved = page.waitForResponse(response => (
        new URL(response.url()).pathname === '/api/skills' && response.request().method() === 'POST'
      ));
      await page.getByRole('button', { name: '保存并创建新版本', exact: true }).click();
      assert.equal((await saved).status(), 200);
      await page.getByText(new RegExp(`Skill「${names.skill}」已保存为 v1。`)).waitFor();

      for (const agentId of ['general-agent', 'data-query-agent']) {
        await chooseAgent(agentId);
        const skills = page.locator('.agent-editor fieldset').filter({ hasText: 'Skills' });
        const checkbox = skills.getByRole('checkbox', { name: new RegExp(names.skill) });
        if (!await checkbox.isChecked()) await checkbox.check();
        await saveAgent();
      }

      await newConversation();
      const answer = await send(`EXT-SKILL-${suffix}：请说明数据血缘的含义。`);
      assert.match(answer.answer || '', /【Skill 验收已生效】/);
      assert.match(answer.answer || '', /结论：/);
      await page.screenshot({ path: path.resolve('.data', 'extension-skill-chat.png'), fullPage: true });
      return { bound_agents: ['general-agent', 'data-query-agent'], answer: answer.answer };
    });

    await check('mcp_create_discover_test_bind_and_chat_use', async () => {
      await navigateExtensions('MCP');
      await page.getByRole('button', { name: '添加 Server', exact: false }).click();
      await page.getByLabel('名称', { exact: true }).fill(names.mcp);
      await page.getByLabel('说明', { exact: true }).fill('项目自带的只读指标定义 MCP Server，用于浏览器验收。');
      await page.getByLabel('Endpoint', { exact: true }).fill('http://127.0.0.1:18765/mcp');
      await page.getByLabel('认证方式', { exact: true }).selectOption('none');
      const saved = page.waitForResponse(response => (
        new URL(response.url()).pathname === '/api/mcp-servers' && response.request().method() === 'POST'
      ), { timeout: 30000 });
      await page.getByRole('button', { name: '保存配置', exact: true }).click();
      assert.equal((await saved).status(), 200);
      await page.getByText(/自动发现 1 个工具/).waitFor({ timeout: 60000 });
      const tested = page.waitForResponse(response => (
        /\/api\/mcp-servers\/[^/]+\/test$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST'
      ));
      await page.getByRole('button', { name: '测试连接', exact: true }).click();
      assert.equal((await tested).status(), 200);
      await page.getByText('连接成功，发现 1 个工具。', { exact: true }).waitFor({ timeout: 30000 });
      await page.getByText('查询指标定义', { exact: true }).waitFor();

      await chooseAgent('data-query-agent');
      const servers = page.locator('.agent-editor fieldset').filter({ hasText: 'MCP Server' });
      const checkbox = servers.getByRole('checkbox', { name: new RegExp(names.mcp) });
      if (!await checkbox.isChecked()) await checkbox.check();
      await saveAgent();

      await newConversation();
      const answer = await send(`请调用“${names.mcp}”中的“查询指标定义”工具，查询 GMV 的指标口径。`);
      assert.match(answer.answer || '', /已支付订单|amount|退款/);
      const nodes = answer.trace?.steps?.map(step => step.node) || [];
      assert.ok(nodes.includes('mcp_tool'), `Expected mcp_tool trace, got ${JSON.stringify(nodes)}`);
      await page.screenshot({ path: path.resolve('.data', 'extension-mcp-chat.png'), fullPage: true });
      return { discovered_tools: 1, answer: answer.answer, trace_steps: nodes };
    });

    await check('no_browser_runtime_errors', async () => {
      assert.deepEqual(report.page_errors, []);
      return { errors: 0 };
    });
  } finally {
    report.finished_at = new Date().toISOString();
    fs.mkdirSync(path.dirname(output), { recursive: true });
    fs.writeFileSync(output, JSON.stringify(report, null, 2));
    await browser.close();
  }
  if (report.checks.some(item => !item.passed)) process.exitCode = 1;
}

main().catch(error => { console.error(error); process.exitCode = 1; });
