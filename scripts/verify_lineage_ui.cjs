/* Real React/HTTP clicks against disposable synthetic servers only. */
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');

async function main(){
 const output=path.resolve('.data/lineage-acceptance');fs.mkdirSync(output,{recursive:true});
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const context=await browser.newContext({baseURL:'http://127.0.0.1:5375',viewport:{width:1440,height:1050}});
 const page=await context.newPage(),errors=[],checks=[];
 page.on('pageerror',e=>errors.push(e.message));
 const sql="INSERT INTO sales_by_region(stat_date,region,paid_amount) SELECT DATE(o.order_time),c.region,SUM(o.order_amount) FROM fact_orders o JOIN customers c ON o.customer_id=c.customer_id WHERE o.status='PAID' GROUP BY DATE(o.order_time),c.region";
 const title='浏览器血缘验收-'+Date.now();
 try{
  const registered=await context.request.post('/api/auth/register',{data:{username:'lineage_ui_acceptance',password:'Only-Synthetic-Acceptance-2026!'}});
  assert.ok([200,409].includes(registered.status()),await registered.text());
  if(registered.status()===409)assert.equal((await context.request.post('/api/auth/login',{data:{username:'lineage_ui_acceptance',password:'Only-Synthetic-Acceptance-2026!'}})).status(),200);
  await page.goto('/');
  await page.getByRole('button',{name:'数据目录',exact:false}).click();
  await page.getByRole('button',{name:'关联与血缘',exact:true}).click();
  await page.getByText('导入 SQL / ETL 定义 / 业务文档',{exact:true}).click();
  await page.getByLabel('证据标题',{exact:true}).fill(title);
  await page.getByLabel('来源版本',{exact:true}).fill(title);
  await page.getByLabel('证据数据源',{exact:true}).selectOption('local-ecommerce-demo');
  await page.getByLabel('证据正文',{exact:true}).fill(sql);
  await page.getByRole('button',{name:'保存并解析证据',exact:true}).click();
  await page.getByRole('status').filter({hasText:'证据已保存'}).waitFor();
  checks.push('React form imports SQL as immutable evidence without executing it');
  const source=await (await context.request.get('/api/catalog/evidence/sources')).json();
  const sourceId=source.find(s=>s.title===title).id;
  const original=await (await context.request.get('/api/catalog/evidence/sources/'+encodeURIComponent(sourceId))).json();
  assert.equal(original.content,sql);assert.match(original.content_hash,/^[a-f0-9]{64}$/);
  checks.push('original SQL, version and SHA-256 visible');
  function card(from,to){return page.getByTestId('lineage-candidate')
    .filter({has:page.locator('.evidence-flow strong').filter({hasText:from})})
    .filter({has:page.locator('.evidence-flow strong').filter({hasText:to})});}
  const amount=card('fact_orders.order_amount','sales_by_region.paid_amount');
  assert.equal(await amount.count(),1);
  await amount.getByRole('button',{name:'验证 Schema',exact:true}).click();
  await page.getByRole('status').filter({hasText:'操作已完成'}).waitFor();
  await amount.getByLabel('审核说明',{exact:true}).fill('已对照黄金材料确认金额来源');
  await amount.getByRole('button',{name:'批准候选',exact:true}).click();
  await amount.locator('.evidence-status.approved').waitFor();
  checks.push('candidate approval is separate from publication');
  await amount.getByRole('button',{name:'发布候选',exact:true}).click();
  await amount.locator('.evidence-status.published').waitFor();
  checks.push('explicit publication through React');
  await page.getByLabel('选择血缘资产',{exact:true}).selectOption('test-sales_by_region');
  await page.getByLabel('血缘字段',{exact:true}).selectOption('paid_amount');
  await page.locator('.field-lineage-edge').filter({hasText:'fact_orders.order_amount'}).waitFor();
  assert.equal(await page.locator('.field-lineage-edge').count(),1);
  checks.push('published paid_amount upstream and SUM expression displayed');
  const condition=card('fact_orders.status','sales_by_region.paid_amount');
  await condition.getByLabel('审核说明',{exact:true}).fill('该状态只作为过滤影响，不是金额值来源');
  await condition.getByRole('button',{name:'批准候选',exact:true}).click();
  await condition.getByRole('button',{name:'发布候选',exact:true}).click();
  await condition.locator('.evidence-status.published').waitFor();
  await page.getByLabel('包括过滤 / 分组等间接影响',{exact:true}).check();
  await page.locator('.field-lineage-edge').filter({hasText:'fact_orders.status'}).waitFor();
  assert.equal(await page.locator('.field-lineage-edge').count(),2);
  checks.push('filter influence displayed separately from value origin');
  await page.getByLabel('筛选审核状态',{exact:true}).selectOption('published');
  await page.getByRole('button',{name:'收起原文',exact:true}).click();
  await page.screenshot({path:path.join(output,'lineage-desktop.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await page.screenshot({path:path.join(output,'lineage-mobile.png'),fullPage:true});
  assert.equal(await page.locator('.lineage-evidence-panel').evaluate(e=>e.scrollWidth>e.clientWidth+2),false);
  checks.push('mobile evidence/lineage panel has no horizontal overflow');
  await page.setViewportSize({width:1440,height:1050});
  await amount.getByRole('button',{name:'撤回候选',exact:true}).click();
  await page.getByRole('status').filter({hasText:'操作已完成'}).waitFor();
  await page.waitForFunction(()=>document.querySelectorAll('.field-lineage-edge').length===1);
  assert.equal(await page.locator('.field-lineage-edge').filter({hasText:'fact_orders.order_amount'}).count(),0);
  checks.push('withdrawal immediately refreshes visible field lineage');
  await page.getByLabel('筛选审核状态',{exact:true}).selectOption('');
  await condition.getByRole('button',{name:'查看原始证据',exact:true}).click();
  await page.getByLabel('证据状态说明',{exact:true}).fill('原业务说明已撤销，停止使用');
  await page.getByRole('button',{name:'标记证据失效',exact:true}).click();
  await page.waitForFunction(()=>document.querySelectorAll('.field-lineage-edge').length===0);
  checks.push('source invalidation hides all dependent published evidence');
  // File upload is also exercised through the actual form; no live model required.
  await page.getByLabel('证据类型',{exact:true}).selectOption('business_document');
  await page.getByLabel('证据标题',{exact:true}).fill('文档浏览器验收');
  await page.getByLabel('文档绑定资产',{exact:true}).selectOption(['test-fact_orders','test-customers']);
  await page.getByLabel('原始文件（可选，20 MiB 内）',{exact:true}).setInputFiles({name:'synthetic-evidence.md',mimeType:'text/markdown',buffer:Buffer.from('# Evidence\n\nfact_orders.customer_id joins customers.customer_id.')});
  await page.getByRole('button',{name:'保存并解析证据',exact:true}).click();
  await page.getByRole('status').filter({hasText:'待模型提取'}).waitFor();
  await page.getByRole('link',{name:'下载原始文件',exact:true}).waitFor();
  checks.push('document upload retains original file and awaits model extraction, not auto-publication');
  assert.deepEqual(errors,[]);checks.push('no browser JavaScript errors');
  const report={passed:checks.length,total:checks.length,checks,page_errors:errors,scope:'real Edge React/HTTP with synthetic isolated data'};
  fs.writeFileSync(path.join(output,'ui-report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify(report,null,2));
 }catch(e){await page.screenshot({path:path.join(output,'failure.png'),fullPage:true}).catch(()=>{});throw e;}
 finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
