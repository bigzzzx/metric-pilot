/* Browser regression against Vite, with synthetic API data only.
 * NODE_PATH must include an installed Playwright. No real account/model calls.
 */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const baseURL = process.env.SCROLL_TEST_URL || 'http://127.0.0.1:5174';

function history(id) {
  return Array.from({ length: 40 }, (_, i) => ({
    id: `${id}-${i}`, role: i % 2 ? 'assistant' : 'user',
    content: `${id} 消息 ${i}：` + '这是用于滚动测试的合成历史内容。'.repeat(14),
  }));
}

async function run(browser, viewport) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const conversations = ['long-a', 'long-b'].map(id => ({ id, title: id, updated_at: '2026-09-13' }));
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname;
    let data;
    if (path === '/api/auth/me') data = { id: 'synthetic', username: 'scroll-test' };
    else if (path === '/api/conversations') {
      if (route.request().method() === 'POST') {
        data = { id: 'empty', title: '新对话' };
        conversations.unshift(data);
      } else data = conversations;
    } else if (/\/messages$/.test(path)) {
      const id = path.split('/')[3];
      await new Promise(resolve => setTimeout(resolve, 100));
      data = { conversation: { id }, messages: id === 'empty' ? [] : history(id) };
    } else throw Error(`Unexpected API request: ${path}`);
    await route.fulfill({ json: data });
  });
  // A controllable real ReadableStream exercises the application's SSE renderer.
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    window.fetch = (url, options) => {
      if (url !== '/api/chat/stream') return original(url, options);
      return Promise.resolve(new Response(new ReadableStream({
        start(controller) {
          window.pushChatEvent = (name, data) => controller.enqueue(new TextEncoder().encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`));
          window.closeChatStream = () => controller.close();
        },
      }), { headers: { 'Content-Type': 'text/event-stream' } }));
    };
  });
  const metrics = () => page.evaluate(() => {
    const main = document.querySelector('main');
    const el = /auto|scroll/.test(getComputedStyle(main).overflowY) ? main : document.scrollingElement;
    return { top: el.scrollTop, gap: el.scrollHeight - el.scrollTop - el.clientHeight };
  });
  async function bottom() {
    await page.waitForFunction(() => {
      const main = document.querySelector('main');
      if (!main || !document.querySelector('.conversation-list')) return false;
      const el = /auto|scroll/.test(getComputedStyle(main).overflowY) ? main : document.scrollingElement;
      return el.scrollHeight - el.scrollTop - el.clientHeight <= 2;
    });
  }
  async function scrollUp() {
    await page.evaluate(() => {
      const main = document.querySelector('main');
      const el = /auto|scroll/.test(getComputedStyle(main).overflowY) ? main : document.scrollingElement;
      el.scrollTop = 200;
    });
    await page.waitForTimeout(80); // Allow the browser's scroll event to fire.
  }
  try {
    await page.goto(baseURL);
    await bottom();
    assert.ok((await metrics()).top > 1000);
    await page.reload();
    await bottom();
    if (viewport.width > 700) {
      const sidebar = await page.locator('.console-sidebar').boundingBox();
      await scrollUp();
      await page.getByRole('button', { name: 'long-b', exact: true }).click();
      await page.getByText('long-b 消息 39：', { exact: false }).waitFor();
      await bottom();
      await page.getByRole('button', { name: 'long-a', exact: true }).click();
      await page.getByText('long-a 消息 39：', { exact: false }).waitFor();
      await bottom();
      assert.equal((await page.locator('.console-sidebar').boundingBox()).y, sidebar.y);
    }
    await scrollUp();
    await page.getByPlaceholder('发消息给 Data Agent…').fill('测试发送');
    await page.getByRole('button', { name: '发送', exact: true }).click();
    await bottom();
    await page.waitForFunction(() => !!window.pushChatEvent);
    await page.evaluate(() => window.pushChatEvent('delta', { content: '流式内容。'.repeat(900) }));
    await page.locator('.assistant-message').last().getByText('流式内容。'.repeat(900), { exact: true }).waitFor();
    await bottom();
    await scrollUp();
    const before = await metrics();
    await page.evaluate(() => window.pushChatEvent('delta', { content: '新增内容。'.repeat(900) }));
    await page.waitForTimeout(150);
    assert.ok(Math.abs((await metrics()).top - before.top) < 3, 'Streaming pulled reader away from history');
    await page.evaluate(() => {
      const main = document.querySelector('main');
      const el = /auto|scroll/.test(getComputedStyle(main).overflowY) ? main : document.scrollingElement;
      el.scrollTop = el.scrollHeight;
    });
    await page.waitForTimeout(80);
    await page.evaluate(() => {
      window.pushChatEvent('done', { intent: 'CHAT', answer: '最终完整回答。'.repeat(2000) });
      window.closeChatStream();
    });
    await bottom();
    const lastText = await page.locator('.assistant-message .message-text').last().boundingBox();
    const composer = await page.locator('.chat-docked').boundingBox();
    assert.ok(lastText.y + lastText.height <= composer.y, 'Latest text hidden under composer');
    if (viewport.width > 700) await page.getByRole('button', { name: '新对话', exact: true }).click();
    else await page.getByRole('button', { name: '创建新对话', exact: false }).click();
    await page.locator('.home-welcome h1').waitFor();
    assert.equal((await metrics()).top, 0);
    assert.deepEqual(errors, []);
    console.log(`PASS ${viewport.width}px: entry, refresh, ${viewport.width > 700 ? 'switch, sidebar, ' : ''}send, stream-follow, history-reading, resume, empty conversation`);
  } finally { await context.close(); }
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    await run(browser, { width: 1440, height: 900 });
    await run(browser, { width: 390, height: 844 });
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
