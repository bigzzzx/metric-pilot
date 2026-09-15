import React from 'react';

const kindNames = {
  table: '数据表', column: '字段', relation: '血缘关系', metric: '指标口径',
  term: '业务术语', knowledge: '知识文档', catalog: '目录',
  metric_wiki: '指标 Wiki',
};

const nodeNames = {
  router: '问题路由', memory_search: '长期记忆', rag: '检索目录与知识',
  data_query: '生成查询计划', query: '执行只读查询', repair: '修复 SQL',
  data_analysis: '分析查询结果', query_failed: '处理查询异常',
  visualization: '生成结果图表',
  metric_router: '匹配指标口径', metric_plan: '编译指标查询',
};

function SourceList({ sources }) {
  if (!sources?.length) return null;
  return <section><h4>检索依据</h4><div className="trace-source-list">{sources.map(source =>
    <div key={source.id} className="trace-source">
      <span>{kindNames[source.kind] || source.kind}</span><b>{source.title}</b>
      {source.locator && <small>{source.locator}</small>}
    </div>
  )}</div></section>;
}

function VerifiedTables({ tables }) {
  if (!tables?.length) return null;
  return <section><h4>已核实数据表</h4><div className="trace-table-list">{tables.map(table =>
    <code key={`${table.source_id}:${table.asset_id}`}>{table.name}{table.catalog_version ? ` · v${table.catalog_version}` : ''}{table.source_name ? ` · ${table.source_name}` : ''}</code>
  )}</div></section>;
}

function PlanningFacts({ trace }) {
  const hasFacts = trace.metric_refs?.length || trace.filters?.length || trace.join_relationships?.length || trace.time_column;
  if (!hasFacts) return null;
  return <section><h4>查询口径</h4><dl className="trace-facts">
    {trace.metric_refs?.length > 0 && <><dt>指标</dt><dd>{trace.metric_refs.map(item => item.id).join('、')}</dd></>}
    {trace.time_column && <><dt>时间字段</dt><dd><code>{trace.time_column}</code></dd></>}
    {trace.filters?.length > 0 && <><dt>过滤条件</dt><dd>{trace.filters.join('；')}</dd></>}
    {trace.join_relationships?.length > 0 && <><dt>关联</dt><dd>{trace.join_relationships.join('；')}</dd></>}
  </dl></section>;
}

function ExecutionPath({ trace }) {
  if (!trace.nodes?.length && !trace.execution) return null;
  return <section><h4>执行过程</h4>
    {trace.nodes?.length > 0 && <div className="trace-node-list">{trace.nodes.map((node, index) =>
      <span key={`${node.node}:${index}`} className={node.status}>{nodeNames[node.node] || node.label || node.node}<small>{node.duration_ms == null ? '' : `${node.duration_ms} ms`}</small></span>
    )}</div>}
    {trace.execution && <p className={trace.execution.error_message ? 'trace-error' : ''}>
      {trace.execution.error_message || (trace.execution.result_reused ? '复用上一次查询结果，本轮未执行 SQL。' : trace.execution.status === 'success' ? `查询成功，返回 ${trace.execution.row_count || 0} 行` : '尚未执行数据查询。')}
    </p>}
  </section>;
}

export default function QueryTraceDetails({ trace }) {
  if (!trace) return null;
  const sourceCount = trace.sources?.length || 0;
  const tableCount = trace.verified_tables?.length || 0;
  return <details className="query-trace"><summary>查看依据与执行过程 <span>{sourceCount} 条依据 · {tableCount} 张表</span></summary>
    <div className="query-trace-body">
      <div className="trace-overview"><span>路由：{trace.route || 'data_query'}</span>{trace.contextual_follow_up && <span>已结合上文</span>}</div>
      {trace.query_mode && <p>查询方式：{trace.query_mode === 'text_to_metrics' ? '已治理指标查询（Text-to-Metrics）' : '探索查询（Text-to-SQL）'}</p>}
      <SourceList sources={trace.sources} />
      <VerifiedTables tables={trace.verified_tables} />
      <PlanningFacts trace={trace} />
      <ExecutionPath trace={trace} />
    </div>
  </details>;
}
