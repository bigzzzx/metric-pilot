import test from 'node:test';
import assert from 'node:assert/strict';
import {chartOption,numericValue} from '../src/components/chat/visualizationOptions.js';

test('chronological order, decimal conversion and null gaps without source mutation',()=>{
  const rows=[{day:'2026-09-02',sales:null},{day:'2026-09-01',sales:'12.50'}];
  const option=chartOption({x:'day',y:['sales']},rows,'line');
  assert.deepEqual(option.dataset.source,[{day:'2026-09-01',sales:12.5},{day:'2026-09-02',sales:null}]);
  assert.equal(rows[0].day,'2026-09-02');
  assert.equal(option.series[0].connectNulls,false);
  assert.equal(option.tooltip.renderMode,'richText');
  assert.equal(numericValue(false),null);
  assert.equal(numericValue('Infinity'),null);
});

test('horizontal and scatter axis mappings reference actual fields',()=>{
  const spec={x:'region',y:['sales']};
  const option=chartOption(spec,[{region:'east',sales:10}],'horizontal_bar');
  assert.deepEqual(option.series[0].encode,{x:'sales',y:'region'});
  const scatter=chartOption({x:'price',y:['rate']},[{price:'2.5',rate:'0.4'}],'scatter');
  assert.equal(scatter.xAxis.type,'value');
  assert.deepEqual(scatter.dataset.source,[{price:2.5,rate:0.4}]);
});
