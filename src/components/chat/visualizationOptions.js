export const chartNames = {card:'指标卡',line:'折线图',bar:'柱状图',horizontal_bar:'条形图',pie:'环形图',scatter:'散点图',table:'数据表'};

export function numericValue(value) {
  if (value == null || typeof value === 'boolean' || String(value).trim() === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

export function chartOption(spec, rows, type) {
  let data = rows.map(row => ({...row}));
  if (type === 'line' && data.every(row => /^\d{4}-\d{2}/.test(String(row[spec.x])))) {
    data.sort((a,b) => String(a[spec.x]).localeCompare(String(b[spec.x])));
  }
  const measures = spec.y || [];
  const source = data.map(row => Object.fromEntries(Object.entries(row).map(([key,value]) =>
    [key, measures.includes(key) || (type === 'scatter' && key === spec.x) ? numericValue(value) : value])));
  const common = {
    backgroundColor:'#ffffff', color:['#4169e1','#14a89b','#eea644'],
    animation:false, aria:{enabled:true},
    tooltip:{trigger:type === 'pie' || type === 'scatter' ? 'item' : 'axis',renderMode:'richText',confine:true},
    legend:{bottom:0,type:'scroll'}, dataset:{source},
  };
  if (type === 'pie') return {...common, series:[{name:measures[0],type:'pie',radius:['42%','68%'],
    encode:{itemName:spec.x,value:measures[0]},label:{formatter:'{b}: {d}%'}}]};
  const horizontal = type === 'horizontal_bar';
  const xAxis = {type:type === 'scatter' ? 'value' : 'category',name:spec.x, nameLocation:'middle',nameGap:32,
    axisLabel:{hideOverlap:true,overflow:'truncate',width:90}};
  const yAxis = {type:'value',scale:false};
  return {...common, grid:{left:12,right:24,top:25,bottom:70,containLabel:true},
    xAxis:horizontal ? {...yAxis} : xAxis,
    yAxis:horizontal ? {...xAxis,nameLocation:'end',nameGap:12,inverse:true} : yAxis,
    series:measures.map(key => ({name:key,type:type === 'horizontal_bar' ? 'bar' : type,
      encode:{x:horizontal ? key : spec.x,y:horizontal ? spec.x : key},
      connectNulls:false,barMaxWidth:38,symbolSize:type === 'scatter' ? 9 : 5})),
    dataZoom:data.length > 16 ? [{type:'inside',...(horizontal ? {yAxisIndex:0} : {xAxisIndex:0})}] : [],
  };
}
