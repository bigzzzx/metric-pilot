/* Real React UI acceptance against the disposable server only. */
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const browser = await chromium.launch({channel:'msedge',headless:true});
  const context = await browser.newContext({baseURL:'http://127.0.0.1:5375',viewport:{width:1440,height:1000}});
  const page = await context.newPage();
  const errors = [], checks = [];
  const jobName = '前端验收订单日汇总-' + Date.now();
  const output = path.resolve('.data/data-platform-acceptance');
  fs.mkdirSync(output,{recursive:true});
  page.on('pageerror', error => errors.push(error.message));
  try {
    const registered = await context.request.post('/api/auth/register', {data:{username:'platform_ui_acceptance',password:'Only-Synthetic-Acceptance-2026!'}});
    assert.ok([200,409].includes(registered.status()),await registered.text());
    if (registered.status() === 409) {
      const login = await context.request.post('/api/auth/login',{data:{username:'platform_ui_acceptance',password:'Only-Synthetic-Acceptance-2026!'}});
      assert.equal(login.status(),200);
    }
    await page.goto('http://127.0.0.1:5375');
    await page.getByRole('button',{name:'数据目录',exact:false}).click();
    await page.getByRole('button',{name:'数据服务',exact:true}).click();
    await page.getByLabel('指标',{exact:true}).selectOption('metric-demo-paid');
    await page.getByLabel('起始日期（含）').fill('2026-08-01');
    await page.getByLabel('结束日期（不含）').fill('2026-09-01');
    await page.getByRole('button',{name:'查询指标',exact:true}).click();
    await page.locator('.service-result').waitFor();
    assert.match(await page.locator('.service-result').innerText(),/150/);
    checks.push('metric selection, real query and result rendered');
    assert.match(await page.locator('.service-result').innerText(),/尚无可验证的更新时间/);
    checks.push('unknown freshness shown explicitly');
    await page.screenshot({path:path.join(output,'metric-service.png'),fullPage:true});
    await page.getByRole('button',{name:'数据生产',exact:true}).click();
    await page.getByText('登记日汇总任务',{exact:true}).click();
    await page.getByLabel('任务名称',{exact:true}).fill(jobName);
    await page.getByLabel('输入数据资产',{exact:true}).selectOption('asset-orders');
    await page.getByLabel('订单日期',{exact:true}).selectOption('order_date');
    await page.getByLabel('订单金额',{exact:true}).selectOption('amount');
    await page.getByLabel('订单状态',{exact:true}).selectOption('status');
    await page.getByLabel('订单唯一标识',{exact:true}).selectOption('order_id');
    await page.getByRole('button',{name:'登记任务',exact:true}).click();
    await page.getByText('任务已登记，可以选择日期发起运行。',{exact:true}).waitFor();
    checks.push('job registration from React form');
    const jobId = await page.getByLabel('已登记任务',{exact:true}).inputValue();
    await page.getByLabel('起始日期（含）').fill('2026-08-01');
    await page.getByLabel('结束日期（不含）').fill('2026-08-02');
    await page.getByRole('button',{name:'申请运行',exact:true}).click();
    await page.getByRole('button',{name:'批准本次',exact:true}).waitFor();
    const before = await context.request.get('/api/data-services/production/runs?job_id='+jobId);
    assert.deepEqual(await before.json(),[]);
    checks.push('no production execution before approval');
    await page.getByRole('button',{name:'批准本次',exact:true}).click();
    await page.getByText('已批准并执行',{exact:true}).waitFor({timeout:30000});
    const runCard = page.locator('.production-runs article').filter({hasText:jobName});
    await runCard.locator('.production-status.succeeded').waitFor();
    assert.match(await runCard.innerText(),/1 笔订单/);
    checks.push('approval resumes real write and displays successful run');
    await runCard.getByText('查看步骤和执行记录',{exact:true}).click();
    assert.match(await runCard.innerText(),/主键、金额与汇总对账/);
    checks.push('actual quality and transformation steps visible');
    await page.screenshot({path:path.join(output,'production-run.png'),fullPage:true});
    const history = await (await context.request.get('/api/data-services/production/runs?job_id='+jobId)).json();
    const jobs = await (await context.request.get('/api/data-services/production/jobs')).json();
    const asset = await (await context.request.get('/api/catalog/assets/'+jobs.find(job => job.id === jobId).target_asset_id)).json();
    assert.equal(asset.publish_status,'draft');
    checks.push('production output requires catalog publication');
    assert.equal(history[0].status,'succeeded');
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(output,'production-mobile.png'),fullPage:true});
    const overflow = await page.locator('.data-service-panel').evaluate(element => element.scrollWidth > element.clientWidth + 2);
    assert.equal(overflow,false);
    checks.push('mobile production panel has no horizontal overflow');
    assert.deepEqual(errors,[]);
    checks.push('no browser runtime errors');
    console.log(JSON.stringify({passed:checks.length,checks,page_errors:errors},null,2));
  } catch (error) {
    await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});
    throw error;
  } finally { await browser.close(); }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
