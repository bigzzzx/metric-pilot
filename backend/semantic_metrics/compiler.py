"""Compile a validated metric request. Model output can never supply SQL."""
import re
from datetime import date, timedelta
from typing import Literal
import sqlglot
from sqlglot import exp
from pydantic import Field, model_validator
from backend.semantic_metrics.models import ContractModel


class MetricRequest(ContractModel):
    start_date: date | None = None
    end_date: date | None = None
    dimensions: list[str] = Field(default_factory=list,max_length=3)
    clarification: str = Field(default='',max_length=1000)
    intent: Literal['query','explain'] = 'query'
    @model_validator(mode='after')
    def ordered_dates(self):
        if bool(self.start_date)!=bool(self.end_date) or (self.start_date and self.start_date>=self.end_date):
            raise ValueError('Use a valid inclusive start / exclusive end date range')
        if len(self.dimensions)!=len(set(self.dimensions)):
            raise ValueError('Duplicate dimensions')
        return self


def quote(name):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',name):
        raise ValueError('Invalid identifier')
    return '"'+name+'"'


def literal(value):
    return "'"+str(value).replace("'","''")+"'"


def fast_request(question,metric,today=None):
    """Consume the entire supported grammar; extra filters never disappear."""
    remaining=question
    for alias in sorted({metric.name,metric.display_name,*metric.aliases},key=len,reverse=True):
        remaining=re.sub(re.escape(alias),' ',remaining,flags=re.I)
    dimensions=[]
    for key,dim in metric.contract.dimensions.items():
        for alias in sorted({key,dim.label,*dim.aliases},key=len,reverse=True):
            if alias and re.search('(按|每)'+re.escape(alias),remaining):
                remaining=re.sub('(按|每)'+re.escape(alias),' ',remaining)
                if key not in dimensions: dimensions.append(key)
    periods=list(re.finditer(r'(?<!\d)(20\d{2})[年/-](\d{1,2})(?:[月/-](\d{1,2})日?)?(?:月)?(?!\d)',remaining))
    start=end=None
    try:
        if len(periods)>1: return None
        if periods:
            m=periods[0]; year,month=int(m[1]),int(m[2])
            start=date(year,month,int(m[3]) if m[3] else 1)
            end=start+timedelta(days=1) if m[3] else date(year+(month==12),month%12+1,1)
            remaining=remaining[:m.start()]+' '+remaining[m.end():]
    except ValueError:
        return MetricRequest(clarification='日期无效，请确认统计时间。')
    remaining=re.sub(r'查询|统计|计算|请问|请|帮我|一下|的|是多少|多少|合计|总额|总计|金额|[\s，,。.?？]','',remaining)
    if remaining: return None
    return MetricRequest(start_date=start,end_date=end,dimensions=dimensions)


def validate_expression(contract,columns):
    dialect='postgres' if contract.dialect=='postgresql' else contract.dialect
    trees=sqlglot.parse(contract.measure_expression,read=dialect)
    if len(trees)!=1 or trees[0] is None: raise ValueError('Invalid measure expression')
    tree=trees[0]
    allowed=(exp.Column,exp.Identifier,exp.Sum,exp.Count,exp.Avg,exp.Min,exp.Max,exp.Distinct,
             exp.Add,exp.Sub,exp.Mul,exp.Div,exp.Paren,exp.Nullif,exp.Literal,exp.Star)
    if any(not isinstance(node,allowed) for node in tree.walk()) or not tree.find(exp.AggFunc):
        raise ValueError('Measure must be an approved aggregate expression')
    if any(c.table or c.name not in columns for c in tree.find_all(exp.Column)):
        raise ValueError('Measure references an unavailable column')
    return tree.sql(dialect=dialect)


def compile_metric_query(question,resolution,*,request=None,today=None,columns=None):
    if resolution.status!='matched': return {'answer':resolution.message,'sql':None}
    metric=resolution.metrics[0]; contract=metric.contract
    request=request or fast_request(question,metric,today)
    refs=[{'id':metric.document_id,'version':metric.version}]
    def stop(message): return {'answer':message,'sql':None,'metric_refs':refs}
    if request is None: return stop('请明确统计时间和分组维度；当前请求还包含未解析的条件。')
    if request.clarification: return stop(request.clarification)
    if request.intent=='explain':
        return stop(metric.display_name+'：'+metric.description+'\n'+str(metric.wiki.get('content','')))
    if contract.requires_time_range and not request.start_date:
        return stop('已选定“'+metric.display_name+'”，请补充统计时间。')
    if request.start_date and not contract.time_column:
        return stop('该指标没有已治理的时间字段，不能应用本次时间条件。')
    if any(key not in contract.dimensions for key in request.dimensions):
        return stop('请求的分组维度尚未在指标契约中定义。')
    if columns is None: raise ValueError('Verified columns are required')
    needed={f.column for f in contract.required_filters}|{contract.dimensions[k].column for k in request.dimensions}
    if contract.time_column: needed.add(contract.time_column)
    if not needed<=set(columns): raise ValueError('Metric contract references unavailable columns')
    expression=validate_expression(contract,set(columns))
    select=[]; groups=[]
    for key in request.dimensions:
        dim=contract.dimensions[key]; value=quote(dim.column)
        if dim.kind=='time': value=f'CAST({value} AS DATE)' if contract.dialect=='postgresql' else f'DATE({value})'
        select.append(value+' AS '+quote(key)); groups.append(value)
    select.append(expression+' AS '+quote(contract.measure_alias))
    filters=[quote(f.column)+' '+f.operator+' '+literal(f.value) for f in contract.required_filters]
    if request.start_date:
        filters += [quote(contract.time_column)+' >= '+literal(request.start_date.isoformat()),
                    quote(contract.time_column)+' < '+literal(request.end_date.isoformat())]
    sql='SELECT '+', '.join(select)+' FROM '+'.'.join(quote(x) for x in [contract.namespace,contract.table] if x)
    if filters: sql+=' WHERE '+' AND '.join(filters)
    if groups: sql+=' GROUP BY '+', '.join(groups)+' ORDER BY '+', '.join(quote(k) for k in request.dimensions)
    return dict(answer='按已认证口径 '+metric.display_name+' 查询。',sql=sql,source_id=contract.source_id,
                metric_refs=refs,time_column=contract.time_column,filters=filters,join_relationships=[],schema_request=[])
