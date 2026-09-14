// Real browser UI regression with synthetic API responses; no account or model calls.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const baseURL = process.env.SCROLL_TEST_URL || 'http://127.0.0.1:5174';
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64');

async function run(browser, width) {
  const context = await browser.newContext({ viewport: { width, height: 900 } });
  const page = await context.newPage();
  const errors = [], files = [], messages = [], requests = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route(url => url.pathname.startsWith('/api/'), async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return route.fulfill({ json: { id:'test', username:'attachments-test' } });
    if (path === '/api/conversations') return route.fulfill({ json: [{ id:'test', title:'附件测试' }] });
    if (path.endsWith('/messages')) return route.fulfill({ json: { conversation:{id:'test'}, messages } });
    if (path.endsWith('/attachments/upload')) {
      const body = route.request().postDataBuffer().toString();
      const name = body.match(/filename="([^"]+)"/)?.[1];
      const file = { id:`f${files.length}`, name, kind:name.endsWith('.png')?'image':'document', byte_size:10 };
      files.push(file);
      return route.fulfill({ json:file });
    }
    if (path.includes('/attachments/')) {
      if (route.request().method() === 'DELETE') return route.fulfill({ json:{deleted:true} });
      return route.fulfill({ body:png, contentType:'image/png' });
    }
    if (path === '/api/chat/stream') {
      const body = route.request().postDataJSON(); requests.push(body);
      messages.push({id:'u'+messages.length,role:'user',content:body.message,payload:{attachments:files.filter(file=>body.attachment_ids.includes(file.id))}});
      const answer = {intent:'CHAT',answer:'已根据当前会话附件回答。'};
      messages.push({id:'a'+messages.length,role:'assistant',content:answer.answer,payload:answer});
      return route.fulfill({body:`event: done\ndata: ${JSON.stringify(answer)}\n\n`,contentType:'text/event-stream'});
    }
    return route.fulfill({ status:404, json:{detail:'unexpected route'} });
  });
  try {
    await page.goto(baseURL);
    await page.getByRole('button',{name:'添加图片或文件'}).waitFor();
    await page.locator('input[type=file]').setInputFiles([
      {name:'chart.png',mimeType:'image/png',buffer:png},
      {name:'metrics.txt',mimeType:'text/plain',buffer:Buffer.from('GMV includes paid orders.')},
    ]);
    await page.locator('.pending-attachments a').nth(1).waitFor();
    await page.getByPlaceholder('发消息给 Data Agent…').fill('解释这两个附件');
    await page.getByRole('button',{name:'发送',exact:true}).click();
    await page.getByText('已根据当前会话附件回答。',{exact:true}).waitFor();
    assert.deepEqual(requests[0].attachment_ids,['f0','f1']);
    assert.equal(await page.locator('.user-bubble .attachment-links a').count(),2);
    assert.equal(await page.locator('.user-bubble img').count(),1);
    await page.reload();
    await page.locator('.user-bubble .attachment-links a').nth(1).waitFor();
    assert.equal(await page.locator('.user-bubble img').count(),1);
    await page.locator('input[type=file]').setInputFiles({name:'remove.txt',mimeType:'text/plain',buffer:Buffer.from('draft')});
    await page.getByRole('button',{name:'移除 remove.txt'}).click();
    await page.waitForFunction(()=>!document.querySelector('.pending-attachments'));
    assert.deepEqual(errors,[]);
    console.log(`PASS ${width}px: upload image and text, send ids, preview, restore after reload, remove draft`);
  } finally { await context.close(); }
}
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{await run(browser,1440);await run(browser,390);}finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
