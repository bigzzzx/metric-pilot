/* Browser presentation/wiring test. Recovery/index payloads mocked; HTTP safety
 * is verified separately by verify_runtime_reliability.py. Disposable API only. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');

async function main(){
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const context=await browser.newContext({baseURL:'http://127.0.0.1:5375',viewport:{width:1440,height:1000}});
 const page=await context.newPage(), errors=[], checks=[];
 page.on('pageerror',e=>errors.push(e.message));
 let recovered=false,retried=false;
 const run={id:'synthetic-interrupted-run',job_id:'synthetic-job',start_date:'2026-08-01',end_date:'2026-08-02',started_at:'2026-11-06',status:'unknown',recovery_required:true,steps:[],result:{},error_message:'提交状态待核对，不能自动重试'};
 try{
  const login=await context.request.post('/api/auth/login',{data:{username:'platform_ui_acceptance',password:'Only-Synthetic-Acceptance-2026!'}});
  assert.equal(login.status(),200,'Run verify_data_platform_ui.cjs first on disposable servers');
  await page.route('**/api/data-services/production/jobs',r=>r.fulfill({json:[{id:'synthetic-job',name:'中断恢复界面验收',enabled:true,can_execute:true}]}));
  await page.route('**/api/data-services/production/runs',r=>r.fulfill({json:[recovered?{...run,status:'succeeded',recovery_required:false}:run]}));
  await page.route('**/api/data-services/production/runs/synthetic-interrupted-run/recover',r=>{
    assert.equal(r.request().method(),'POST');recovered=true;return r.fulfill({json:{status:'succeeded',message:'提交凭证已核对，未重新执行写入。'}});
  });
  await page.goto('/');
  await page.getByRole('button',{name:'数据目录',exact:false}).click();
  await page.getByRole('button',{name:'数据生产',exact:true}).click();
  await page.getByText('结果待确认',{exact:true}).waitFor();checks.push('unknown is visible rather than reported as failed');
  await page.getByRole('button',{name:'核对中断并恢复',exact:true}).click();
  await page.getByRole('status').filter({hasText:'提交凭证已核对'}).waitFor();
  assert.ok(recovered);assert.equal(await page.getByRole('button',{name:'核对中断并恢复',exact:true}).count(),0);
  checks.push('recovery button posts correct endpoint and refreshes terminal state');
  await page.route('**/api/catalog/evidence/index/jobs',r=>r.fulfill({json:{health:{pending:retried?1:0,running:0,failed:retried?0:1},all_jobs:[{id:'synthetic-index-job',kind:'catalog',status:retried?'pending':'failed',attempts:retried?0:5}]}}));
  await page.route('**/api/catalog/evidence/index/retry/catalog/synthetic-index-job',r=>{
    assert.equal(r.request().method(),'POST');retried=true;return r.fulfill({json:{status:'pending',message:'索引已重新排队'}});
  });
  await page.getByRole('button',{name:'关联与血缘',exact:true}).click();
  await page.getByRole('status').filter({hasText:'1 个达到重试上限'}).waitFor();
  await page.getByText('查看最近索引任务',{exact:true}).click();
  await page.getByRole('button',{name:'重试该索引任务',exact:true}).click();
  await page.getByRole('status').filter({hasText:'1 个等待重试'}).waitFor();assert.ok(retried);
  checks.push('dead-letter details and per-kind retry refresh through React');
  await page.setViewportSize({width:390,height:844});
  fs.mkdirSync('.data/runtime-acceptance',{recursive:true});
  await page.screenshot({path:'.data/runtime-acceptance/retry-mobile.png',fullPage:false});
  const overflow=await page.locator('.lineage-evidence-panel').evaluate(e=>e.scrollWidth>e.clientWidth+2);
  if(overflow) console.log(await page.locator('.lineage-evidence-panel').evaluate(e=>({width:e.clientWidth,scroll:e.scrollWidth,wide:[...e.querySelectorAll('*')].filter(x=>x.getBoundingClientRect().right>e.getBoundingClientRect().right+2).slice(0,6).map(x=>({tag:x.tagName,class:x.className,width:x.getBoundingClientRect().width,text:x.textContent.slice(0,80)}))})));
  assert.equal(overflow,false);checks.push('mobile reliability status has no horizontal overflow');
  assert.deepEqual(errors,[]);checks.push('no browser JavaScript errors');
  const report={passed:checks.length,total:checks.length,checks,mode:'real React/Edge; mocked recovery/index responses, separate HTTP acceptance required'};
  fs.writeFileSync('.data/runtime-acceptance/ui-report.json',JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
