// Real browser rendering, export and history restoration against synthetic responses.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const path=require('node:path');

async function run(browser,width){
  const context=await browser.newContext({viewport:{width,height:900},acceptDownloads:true});
  const page=await context.newPage();
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  const spec={result_id:'trusted-test',chart_type:'line',title:'每日销售额（元）',x:'day',y:['sales'],row_count:3,
    available_types:['line','bar','horizontal_bar','pie','table'],notice:'基于本次查询返回的 3 行；时间范围、筛选和统计口径以查询 SQL 为准。'};
  const payload={answer:'8 月 3 日销售额最高，为 240 元。',intent:'DATA_QUERY',sql:'SELECT day, sales FROM synthetic',
    result:[{day:'2026-08-01',sales:'100.00'},{day:'2026-08-02',sales:'180.00'},{day:'2026-08-03',sales:'240.00'}],
    visualizations:[spec]};
  const messages=[{id:'u',role:'user',content:'查看销售趋势'},
    {id:'a',role:'assistant',content:payload.answer,payload}];
  await page.route(url=>url.pathname.startsWith('/api/'),route=>{
    const url=new URL(route.request().url());
    if(url.pathname==='/api/auth/me')return route.fulfill({json:{id:'test',username:'visual-test'}});
    if(url.pathname==='/api/conversations')return route.fulfill({json:[{id:'test',title:'图表测试'}]});
    if(url.pathname.endsWith('/messages'))return route.fulfill({json:{conversation:{id:'test'},messages}});
    return route.fulfill({status:404,json:{detail:'unexpected'}});
  });
  try{
    await page.goto('http://127.0.0.1:5174');
    await page.locator('.query-chart canvas').waitFor();
    assert.equal(await page.getByLabel('展示方式').inputValue(),'line');
    for(const type of ['bar','horizontal_bar','pie']){
      await page.getByLabel('展示方式').selectOption(type);
      await page.locator('.query-chart canvas').waitFor();
    }
    await page.getByText('占比分母仅为当前返回行合计，不代表全量业务数据。',{exact:true}).waitFor();
    const downloaded=page.waitForEvent('download');
    await page.getByRole('button',{name:'导出图片'}).click();
    const file=await downloaded;
    await file.saveAs(path.join('.data','visualization-export-'+width+'.png'));
    assert.equal(await file.failure(),null);
    await page.getByLabel('展示方式').selectOption('table');
    assert.equal(await page.locator('.query-visualization tbody tr').count(),3);
    await page.getByText('180.00',{exact:true}).waitFor();
    await page.reload();
    await page.locator('.query-chart canvas').waitFor();
    assert.equal(await page.getByLabel('展示方式').inputValue(),'line');
    await page.locator('.query-visualization').screenshot({path:path.join('.data','visualization-'+width+'.png')});
    const box=await page.locator('.query-visualization').boundingBox();
    assert.ok(box.width<=width && box.x>=0,'chart overflow');
    payload.result=[{price:12,rate:0.3},{price:22,rate:0.4}];
    payload.visualizations=[{...spec,result_id:'scatter-test',chart_type:'scatter',x:'price',y:['rate'],available_types:['scatter','table']}];
    await page.reload();await page.locator('.query-chart canvas').waitFor();
    assert.equal(await page.getByLabel('展示方式').inputValue(),'scatter');
    payload.result=[{row_count:1200}];
    payload.visualizations=[{...spec,result_id:'card-test',chart_type:'card',x:null,y:['row_count'],available_types:['card','table']}];
    await page.reload();await page.locator('.metric-card strong').waitFor();
    assert.equal(await page.locator('.metric-card strong').innerText(),'1200');
    await page.getByLabel('展示方式').selectOption('table');
    assert.equal(await page.locator('.query-visualization tbody tr').count(),1);
    assert.deepEqual(errors,[]);
    console.log('PASS '+width+'px: real chart rendering, chart/table switch, PNG export, history restoration, no overflow');
  }finally{await context.close();}
}
(async()=>{
  const browser=await chromium.launch({channel:'msedge',headless:true});
  try{await run(browser,1440);await run(browser,390);}finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
