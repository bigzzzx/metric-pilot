import React, {useEffect, useRef, useState} from 'react';
import * as echarts from 'echarts/core';
import {BarChart, LineChart, PieChart, ScatterChart} from 'echarts/charts';
import {AriaComponent, DatasetComponent, GridComponent, LegendComponent, TooltipComponent, DataZoomComponent} from 'echarts/components';
import {CanvasRenderer} from 'echarts/renderers';
import {chartNames, chartOption} from './visualizationOptions.js';
import './QueryVisualization.css';

echarts.use([BarChart,LineChart,PieChart,ScatterChart,DatasetComponent,GridComponent,LegendComponent,
  TooltipComponent,DataZoomComponent,AriaComponent,CanvasRenderer]);

function ResultTable({rows}) {
  const [page,setPage] = useState(0);
  const columns = [...new Set(rows.flatMap(row => Object.keys(row)))];
  const pageCount = Math.ceil(rows.length / 25);
  useEffect(() => setPage(0), [rows]);
  const cell = value => value == null ? '—' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  return <div className="result-data">
    <div className="table-wrap"><table><thead><tr>{columns.map(key=><th key={key}>{key}</th>)}</tr></thead>
      <tbody>{rows.slice(page*25,(page+1)*25).map((row,index)=><tr key={index}>{columns.map(key=><td key={key}>{cell(row[key])}</td>)}</tr>)}</tbody></table></div>
    {pageCount > 1 && <div className="result-pagination"><button disabled={page===0} onClick={()=>setPage(page-1)}>上一页</button><span>第 {page+1} / {pageCount} 页 · {rows.length} 行</span><button disabled={page+1>=pageCount} onClick={()=>setPage(page+1)}>下一页</button></div>}
  </div>;
}

export default function QueryVisualization({rows, spec, notice}) {
  const [type,setType] = useState(spec?.chart_type || 'table');
  const [error,setError] = useState('');
  const container = useRef(null), instance = useRef(null);
  useEffect(()=>{setType(spec?.chart_type || 'table');setError('');},[spec?.result_id,spec?.chart_type]);
  const graphical = spec && !['table','card'].includes(type);
  useEffect(()=>{
    if (!graphical || !container.current) return;
    let chart, observer;
    try {
      chart = echarts.init(container.current);
      instance.current = chart;
      chart.setOption(chartOption(spec,rows,type));
      observer = new ResizeObserver(()=>chart.resize());
      observer.observe(container.current);
    } catch (failure) {setError('图表暂不可用，已保留查询数据。');setType('table');}
    return ()=>{observer?.disconnect();chart?.dispose();instance.current=null;};
  },[graphical,spec,rows,type]);
  const exportImage = ()=>{
    if (!instance.current) return;
    const raw = new Image();
    raw.onload = ()=>{
      const canvas = document.createElement('canvas'); canvas.width=raw.width;canvas.height=raw.height+120;
      const ctx=canvas.getContext('2d');ctx.fillStyle='#fff';ctx.fillRect(0,0,canvas.width,canvas.height);
      ctx.fillStyle='#202124';ctx.font='bold 24px sans-serif';ctx.fillText(spec.title,24,38,canvas.width-48);
      ctx.drawImage(raw,0,58);ctx.font='16px sans-serif';
      ctx.fillText('基于当前返回的 '+rows.length+' 行；范围与口径以查询 SQL 为准。',24,canvas.height-32,canvas.width-48);
      if(type==='pie')ctx.fillText('占比分母仅为返回行合计。',24,canvas.height-10,canvas.width-48);
      const link=document.createElement('a');link.download='data-agent-chart.png';link.href=canvas.toDataURL('image/png');link.click();
    };
    raw.src=instance.current.getDataURL({type:'png',pixelRatio:2,backgroundColor:'#fff'});
  };
  if (!rows.length) return null;
  const choices = spec?.available_types || ['table'];
  return <section className="query-visualization" aria-label="查询结果展示">
    <div className="visualization-toolbar"><h3>{spec?.title || '查询结果'}</h3><div>
      {spec && choices.length>1 && <select aria-label="展示方式" value={type} onChange={event=>setType(event.target.value)}>
        {choices.map(value=><option key={value} value={value}>{chartNames[value] || value}</option>)}
      </select>}
      {graphical && <button onClick={exportImage}>导出图片</button>}
    </div></div>
    {type==='card' && spec ? <div className="metric-cards">{spec.y.map(key=><div className="metric-card" key={key}><span>{key}</span><strong>{rows[0]?.[key] == null ? '—' : String(rows[0][key])}</strong></div>)}</div> :
      graphical ? <div ref={container} className="query-chart" role="img" aria-label={spec.title}/> : <ResultTable rows={rows}/>}
    {(notice || spec?.notice || error) && <p className="visualization-notice">{error || notice || spec?.notice}</p>}
    {type==='pie' && spec?.chart_type!=='pie' && <p className="visualization-notice">占比分母仅为当前返回行合计，不代表全量业务数据。</p>}
  </section>;
}
