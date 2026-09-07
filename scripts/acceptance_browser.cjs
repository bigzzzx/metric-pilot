/* Actual browser + HTTP acceptance. Requires a preinstalled Playwright and Edge.
 * Run against scripts/acceptance_server.py only; accounts/data are synthetic.
 * NODE_PATH may point to the bundled runtime packages. No browser download needed.
 */
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..', '.data', 'acceptance-browser');
const baseURL = 'http://127.0.0.1:5374';
const phase = process.argv[2] || 'initial';
const password = 'Acceptance-only-2026!'; // Synthetic local test account only.
const admin = 'acceptance_admin';
const analyst = 'acceptance_analyst';
const report = { phase, started_at: new Date().toISOString(), checks: [], page_errors: [] };
fs.mkdirSync(root, { recursive: true });

async function main() {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ baseURL, viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on('pageerror', error => report.page_errors.push(error.message));
  async function check(name, run) {
    const start = Date.now();
    try {
      const details = await run();
      report.checks.push({ name, passed: true, duration_ms: Date.now() - start, details });
      console.log('PASS', name);
    } catch (error) {
      report.checks.push({ name, passed: false, duration_ms: Date.now() - start, error: error.message });
      await page.screenshot({ path: path.join(root, 'failure-' + report.checks.length + '.png') }).catch(() => {});
      console.log('FAIL', name, error.message.slice(0, 250));
    }
  }
  async function login(request, username, register = false) {
    if (register) {
      const registered = await request.post('/api/auth/register', { data: { username, password } });
      assert.ok([200, 409].includes(registered.status()), await registered.text());
    }
    const response = await request.post('/api/auth/login', { data: { username, password } });
    assert.equal(response.status(), 200, await response.text());
    return response.json();
  }
  async function send(message) {
    const responsePromise = page.waitForResponse(r => r.url().endsWith('/api/chat/stream') && r.request().method() === 'POST', { timeout: 100000 });
    responsePromise.catch(() => {}); // A failed click must not leave an unhandled pending wait.
    await page.getByPlaceholder('发消息给 Data Agent…').fill(message);
    await page.getByRole('button', { name: '发送', exact: true }).click();
    const response = await responsePromise;
    const raw = await response.text();
    assert.equal(response.status(), 200, raw);
    const events = raw.split(/\r?\n\r?\n/).map(block => ({
      name: block.match(/^event:\s*(.+)$/m)?.[1]?.trim(),
      raw: block.match(/^data:\s*(.+)$/m)?.[1],
    })).filter(item => item.raw).map(item => ({ name: item.name, data: JSON.parse(item.raw) }));
    const failure = events.find(item => item.name === 'error');
    assert.ok(!failure, JSON.stringify(failure?.data));
    const done = events.find(item => item.name === 'done');
    assert.ok(done, 'No terminal SSE event');
    await page.waitForFunction(() => !document.querySelector('.stream-status'));
    return { ...done.data, delta_count: events.filter(item => item.name === 'delta').length };
  }
  async function newConversation() {
    const created = page.waitForResponse(r => r.url().endsWith('/api/conversations') && r.request().method() === 'POST');
    await page.getByRole('button', { name: '创建新对话', exact: false }).click();
    await (await created).finished();
    await page.locator('.chat-page .home-welcome').waitFor();
  }
  async function queryConversation() {
        await newConversation();
        const august = await send('查询orders表，以order_date为日期筛选字段，计算2026年8月status为PAID的amount整月合计，只返回一行总额，不按天分组，排除其他状态。');
        assert.ok(august.sql && august.result?.length);
        assert.ok(Object.values(august.result[0]).includes(150), JSON.stringify(august.result));
        const september = await send('那9月呢？');
        assert.ok(Object.values(september.result?.[0] || {}).includes(300), JSON.stringify(september));
        const concept = await send('刚才的PAID是什么意思？');
        assert.ok(/支付|付款/.test(concept.answer));
        const joke = await send('先不聊数据了，讲一个简短的笑话。');
        assert.equal(joke.intent, 'CHAT');
        await page.screenshot({ path: path.join(root, 'context-followup.png'), fullPage: true });
        return { august: august.result, september: september.result, concept_intent: concept.intent, topic_switch: joke.intent };
  }
  try {
    await check('actual_registration_and_browser_login', async () => {
      const user = await login(context.request, admin, phase === 'initial');
      assert.equal(user.is_catalog_admin, true);
      await context.request.post('/api/auth/logout');
      await page.goto(baseURL);
      await page.locator('input[name=username]').fill(admin);
      await page.locator('input[name=password]').fill(password);
      await page.getByRole('button', { name: '登录', exact: true }).click();
      await page.locator('.console-sidebar').waitFor();
      await page.getByPlaceholder('发消息给 Data Agent…').waitFor();
      assert.equal(await page.getByRole('button', { name: '系统管理', exact: false }).count(), 1);
    });
    if (phase === 'context') {
      await check('query_context_followup_and_topic_switch', queryConversation);
    } else if (phase === 'initial') {
      await check('extension_tabs_repeated_navigation', async () => {
        await page.getByRole('button', { name: '智能体扩展', exact: false }).click();
        for (let round = 0; round < 8; round++) {
          for (const label of ['工具', 'MCP', 'Skills', '知识库']) {
            await page.getByRole('tab', { name: label, exact: true }).click();
            assert.equal(await page.getByText('页面暂时无法显示', { exact: true }).count(), 0);
          }
        }
        assert.deepEqual(report.page_errors, []);
        await page.screenshot({ path: path.join(root, 'extensions.png'), fullPage: true });
        return { tab_switches: 32 };
      });
      await check('greeting_stream_and_refresh_history', async () => {
        await newConversation();
        const answer = await send('你好');
        assert.ok(answer.delta_count > 0);
        assert.equal(answer.intent, 'CHAT');
        const before = await page.locator('.assistant-message').last().innerText();
        await page.reload();
        await page.locator('.assistant-message').last().waitFor();
        assert.equal(await page.locator('.assistant-message').last().innerText(), before);
        return { delta_count: answer.delta_count };
      });
      await check('query_context_followup_and_topic_switch', queryConversation);
      await check('analyst_role_and_cross_user_isolation', async () => {
        const other = await browser.newContext({ baseURL });
        try {
          const user = await login(other.request, analyst, true);
          assert.equal(user.is_catalog_admin, false);
          assert.equal((await other.request.get('/api/admin/overview')).status(), 403);
          const assetsResponse = await other.request.get('/api/catalog/assets');
          assert.equal(assetsResponse.status(), 200);
          const assetPayload = await assetsResponse.json();
          const assets = Array.isArray(assetPayload) ? assetPayload : assetPayload.items;
          assert.ok(assets.length);
          assert.ok(assets.every(asset => asset.domain_id === 'domain-ecommerce'));
          assert.equal((await other.request.get('/api/catalog/assets/asset-ads_campaign_roi')).status(), 404);
          assert.equal((await other.request.get('/api/catalog/assets/asset-users/sample?limit=100')).status(), 422);
          const sample = await (await other.request.get('/api/catalog/assets/asset-users/sample?limit=5')).json();
          assert.ok(sample.rows.length > 0 && sample.rows.length <= 5);
          assert.ok(sample.rows.every(row => !['示例甲', '示例乙', '示例丙', '示例丁'].includes(row.name)));
          const sourceWrite = await other.request.post('/api/data-sources', { data: { name: 'must-not-create', kind: 'local-sqlite' } });
          assert.equal(sourceWrite.status(), 403);
          const owned = await (await context.request.get('/api/conversations')).json();
          assert.ok(owned.length);
          assert.equal((await other.request.get('/api/conversations/' + owned[0].id + '/messages')).status(), 404);
          assert.deepEqual(await (await other.request.get('/api/conversations')).json(), []);
          const otherPage = await other.newPage();
          await otherPage.goto(baseURL);
          await otherPage.locator('.console-sidebar').waitFor();
          assert.equal(await otherPage.getByRole('button', { name: '系统管理', exact: false }).count(), 0);
          return { admin_api_status: 403, other_conversation_status: 404 };
        } finally { await other.close(); }
      });
      await check('write_request_pauses_for_human_approval', async () => {
        await newConversation();
        const pending = await send('请执行写入操作：把orders表中order_id=8的status更新为CANCELLED。这是隔离演示库测试，请先提交审批，等待我批准后执行。');
        assert.ok(pending.approval?.approval_id, JSON.stringify(pending));
        await page.getByRole('button', { name: '批准本次', exact: true }).waitFor();
        fs.writeFileSync(path.join(root, 'pending-approval.json'), JSON.stringify(pending.approval));
        await page.screenshot({ path: path.join(root, 'approval-pending.png'), fullPage: true });
        return { approval_id: pending.approval.approval_id };
      });
      // Persist only identifiers and public terminal answers, not auth cookies/passwords.
      const summaries = await (await context.request.get('/api/conversations')).json();
      const conversation = await (await context.request.get('/api/conversations/' + summaries[0].id + '/messages')).json();
      fs.writeFileSync(path.join(root, 'expected-history.json'), JSON.stringify(conversation, null, 2));
    } else {
      await check('history_survives_actual_backend_process_restart', async () => {
        const expected = JSON.parse(fs.readFileSync(path.join(root, 'expected-history.json'), 'utf8'));
        const restored = await (await context.request.get('/api/conversations/' + expected.conversation.id + '/messages')).json();
        assert.deepEqual(restored.messages, expected.messages);
        await page.locator('.assistant-message').last().waitFor();
        const last = expected.messages.filter(item => item.role === 'assistant').at(-1);
        if (last.payload?.approval) await page.getByRole('button', { name: '批准本次', exact: true }).waitFor();
        else assert.ok((await page.locator('.assistant-message').last().innerText()).includes(last.content));
        await page.screenshot({ path: path.join(root, 'restart-restored.png'), fullPage: true });
        return { persisted_messages: restored.messages.length };
      });
      await check('browser_approval_after_backend_restart', async () => {
        const pending = JSON.parse(fs.readFileSync(path.join(root, 'pending-approval.json'), 'utf8'));
        await page.getByRole('button', { name: '批准本次', exact: true }).click();
        await page.getByText('已批准并执行', { exact: true }).waitFor();
        const endpoint = '/api/tool-approvals/' + pending.approval_id;
        const stored = await (await context.request.get(endpoint)).json();
        assert.equal(stored.result.status, 'success');
        assert.equal(stored.result.data.affected_rows, 1);
        const repeat = await context.request.post(endpoint + '/decision', { data: { decision: 'approved' } });
        assert.ok([200, 409].includes(repeat.status()));
        await page.reload();
        await page.getByText('已批准并执行', { exact: true }).waitFor();
        assert.equal(await page.getByRole('button', { name: '批准本次', exact: true }).count(), 0);
        await page.screenshot({ path: path.join(root, 'approval-complete.png'), fullPage: true });
        return { affected_rows: 1, repeat_http_status: repeat.status() };
      });
    }
    await check('no_browser_runtime_errors', async () => assert.deepEqual(report.page_errors, []));
  } finally {
    report.finished_at = new Date().toISOString();
    fs.writeFileSync(path.join(root, 'browser-' + phase + '-' + report.started_at.replace(/[:.]/g, '-') + '.json'), JSON.stringify(report, null, 2));
    fs.writeFileSync(path.join(root, 'browser-' + phase + '.json'), JSON.stringify(report, null, 2));
    await browser.close();
  }
  if (report.checks.some(check => !check.passed)) process.exitCode = 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; });
