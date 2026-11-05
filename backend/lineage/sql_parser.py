"""Deterministic, non-executing SQL evidence extraction for SQLGlot 28."""
from __future__ import annotations

import re
import sqlglot
from sqlglot import exp
from sqlglot.lineage import lineage
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope
from sqlglot.schema import MappingSchema

from backend.lineage.catalog import BindingError, resolve_table
from backend.lineage.storage import digest


def _refs(values):
    return [dict(asset_id=a,column=c) for a,c in sorted(set(values))]


def _candidate(kind,left,right,expression,context,locator,observation):
    return dict(kind=kind,from_asset_id=left[0],from_column=left[1],to_asset_id=right[0],to_column=right[1],
                expression=expression,context=context,locator=locator,observation=observation)


def _query(statement):
    target = statement.this if isinstance(statement,(exp.Insert,exp.Create)) else None
    columns = []
    if isinstance(target,exp.Schema):
        columns = [item.name for item in target.expressions]
        target = target.this
    if target is not None and not isinstance(target,exp.Table):
        raise BindingError('target_unresolved','输出不是可绑定的物理表或视图。')
    if isinstance(statement,exp.Create) and statement.kind not in {'TABLE','VIEW'}:
        raise BindingError('unsupported_statement','仅解析 CREATE TABLE/VIEW AS 查询。')
    query = statement.expression if target is not None else statement
    if not isinstance(query,exp.Select):
        raise BindingError('unsupported_statement','本版需要静态 SELECT；动态 SQL、UNION、MERGE、UPDATE/DELETE 等明确不推断。')
    query = query.copy()
    if target is not None and statement.args.get('with_'):
        if query.args.get('with_'):
            raise BindingError('unsupported_cte_scope','不合并两层 WITH 定义，请拆分证据。')
        query.set('with_',statement.args['with_'].copy())
    if any(query.find_all(exp.SetOperation)) or any(cte.args.get('recursive') for cte in query.find_all(exp.With)):
        raise BindingError('unsupported_query','本版暂不支持集合运算或递归 CTE。')
    return query,target,columns


def parse_statement(statement,assets,dialect,statement_index,observation):
    query,target,target_columns = _query(statement)
    target_asset = resolve_table(target,assets) if target is not None else None
    original_scope = build_scope(query)
    if original_scope is None:
        raise BindingError('scope_unresolved','无法构建查询作用域。')
    canonical = {}
    schema = {}
    # Rename only physical sources, not CTE references. Explicit namespaces are
    # checked before normalization so an invalid qualifier cannot be erased.
    for scope in original_scope.traverse():
        for column in scope.columns:
            if column.db or column.catalog:
                source = scope.sources.get(column.table)
                if not isinstance(source,exp.Table) or (column.db and source.db != column.db) or (column.catalog and source.catalog != column.catalog):
                    raise BindingError('qualifier_mismatch','字段命名空间与实际输入表不一致。')
                column.set('db',None); column.set('catalog',None)
        for alias,source in scope.sources.items():
            if not isinstance(source,exp.Table):
                continue
            asset = resolve_table(source,assets)
            name = 'asset_'+digest(asset['id'])[:32]
            if name in canonical and canonical[name]['id'] != asset['id']:
                raise BindingError('identity_collision','资产内部标识冲突，停止解析。')
            canonical[name] = asset
            schema[name] = {k:c['data_type'] for k,c in asset['columns'].items()}
            if not source.alias:
                source.set('alias',exp.TableAlias(this=exp.to_identifier(alias)))
            source.set('this',exp.to_identifier(name)); source.set('db',None); source.set('catalog',None)
    if not canonical:
        raise BindingError('no_bound_inputs','查询没有可绑定的真实输入资产。')
    if len(canonical)>30 or sum(len(v) for v in schema.values())>1500:
        raise BindingError('schema_budget','输入 Schema 超过本次解析预算。')
    query = qualify(query,dialect=dialect,schema=MappingSchema(schema,dialect=dialect,normalize=False),infer_schema=False)
    output_names = [item.alias_or_name for item in query.selects]
    if len(output_names)>200 or (not target_asset and (len(set(output_names)) != len(output_names) or not all(output_names))):
        raise BindingError('output_ambiguous','输出名称重复、缺失或超过 200 列，请显式命名输出字段。')
    if target_asset:
        if not target_columns:
            target_columns = list(target_asset['columns']) if isinstance(statement,exp.Insert) else output_names
        if len(target_columns) != len(output_names) or len(set(target_columns))!=len(target_columns) or any(c not in target_asset['columns'] for c in target_columns):
            raise BindingError('output_mapping_unresolved','输出字段数量/位置与目标真实 Schema 不一致。')
        # INSERT maps by ordinal, not by SELECT aliases. Unique target aliases
        # also allow repeated constants/placeholders without ambiguous lineage().
        query.set('expressions',[exp.alias_(item.this if isinstance(item,exp.Alias) else item,name,copy=False)
            for item,name in zip(query.selects,target_columns)])
    root = build_scope(query)
    scopes = {id(scope.expression):scope for scope in root.traverse()}

    def physical_refs(expression,scope,visited=None):
        visited = set() if visited is None else set(visited)
        found = []
        for column in expression.find_all(exp.Column):
            if column.is_star:
                raise BindingError('star_unresolved','星号未能依据真实 Schema 展开。')
            actual = scope
            parent = column.parent
            while parent is not None:
                if id(parent) in scopes:
                    actual = scopes[id(parent)]; break
                parent = parent.parent
            key = (id(actual),column.table,column.name)
            if key in visited:
                raise BindingError('cyclic_lineage','字段作用域存在循环，停止推断。')
            source = actual.sources.get(column.table)
            parent_scope = actual.parent
            while source is None and parent_scope:
                source = parent_scope.sources.get(column.table); parent_scope = parent_scope.parent
            if isinstance(source,exp.Table):
                asset = canonical.get(source.name)
                if not asset or column.name not in asset['columns']:
                    raise BindingError('column_unresolved','字段无法绑定到输入资产。')
                found.append((asset['id'],column.name))
            elif isinstance(source,Scope):
                selected = next((item for item in source.expression.selects if item.alias_or_name==column.name),None)
                if selected is None:
                    raise BindingError('cte_column_unresolved','CTE/子查询字段无法唯一展开。')
                found.extend(physical_refs(selected,source,visited|{key}))
            else:
                raise BindingError('column_ambiguous','字段来源存在歧义或不支持的表函数。')
        return sorted(set(found))

    influences = []
    joins = []
    all_refs = []
    for scope in root.traverse():
        for name,role in [('where','filter'),('group','group'),('having','having'),('qualify','qualify'),('order','order')]:
            clause = scope.expression.args.get(name)
            if clause is not None:
                refs = physical_refs(clause,scope)
                all_refs.extend(refs)
                influences.append(dict(role=role,expression=clause.sql(dialect=dialect),references=_refs(refs)))
        for join in scope.expression.args.get('joins',[]):
            clause = join.args.get('on')
            if clause is None:
                continue
            references = physical_refs(clause,scope)
            all_refs.extend(references)
            influences.append(dict(role='join',expression=clause.sql(dialect=dialect),references=_refs(references)))
            for equality in clause.find_all(exp.EQ):
                if not isinstance(equality.left,exp.Column) or not isinstance(equality.right,exp.Column):
                    continue
                left,right = physical_refs(equality.left,scope),physical_refs(equality.right,scope)
                if len(left)==len(right)==1 and left != right:
                    joins.append((left[0],right[0],clause.sql(dialect=dialect),str(join.side or join.kind or 'INNER')))

    outputs = []
    all_inputs = {asset['id'] for asset in canonical.values()}
    for index,item in enumerate(query.selects):
        node = lineage(item.alias_or_name,query,scope=root,trim_selects=False,dialect=dialect)
        values = []
        chain = []
        for step in node.walk():
            if isinstance(step.expression,exp.Table):
                asset = canonical.get(step.expression.name)
                column = sqlglot.parse_one(step.name,into=exp.Column,dialect=dialect).name
                if not asset or column not in asset['columns']:
                    raise BindingError('lineage_leaf_unresolved','字段血缘叶节点无法绑定。')
                values.append((asset['id'],column))
            else:
                chain.append(step.expression.sql(dialect=dialect))
        all_refs.extend(values)
        outputs.append(dict(name=item.alias_or_name,target_column=target_columns[index] if target_asset else None,
            expression=item.sql(dialect=dialect),expression_chain=chain,value_sources=_refs(values),
            row_dependencies=sorted(all_inputs) if item.find(exp.Count) and not values else []))
    context = dict(all_references=_refs(all_refs),influences=influences,
                   target_asset_id=target_asset['id'] if target_asset else None,
                   source_asset_ids=sorted(all_inputs),statement_kind=statement.key)
    if len(context['all_references'])>100 or len(influences)>60:
        raise BindingError('lineage_budget','字段引用或条件超过单条证据预算，请拆分查询。')
    candidates = []
    locator = dict(statement_index=statement_index)
    for left,right,condition,join_kind in joins:
        candidates.append(_candidate('join',left,right,condition,{**context,'sql_join_kind':join_kind},locator,observation))
    if target_asset:
        for aid in sorted(all_inputs):
            candidates.append(_candidate('table_transform',(aid,''),(target_asset['id'],''),
                'INSERT SELECT' if isinstance(statement,exp.Insert) else 'CREATE AS',context,locator,observation))
        for index,output in enumerate(outputs):
            loc = {**locator,'output_index':index}
            ctx = {**context,'expression_chain':output['expression_chain']}
            target_ref = (target_asset['id'],output['target_column'])
            for value in output['value_sources']:
                candidates.append(_candidate('column_value',(value['asset_id'],value['column']),target_ref,
                    output['expression'],ctx,loc,observation))
            for influence in influences:
                for ref in influence['references']:
                    candidates.append(_candidate('column_influence',(ref['asset_id'],ref['column']),target_ref,
                        influence['expression'],{**ctx,'influence_role':influence['role']},loc,observation))
            for aid in output['row_dependencies']:
                candidates.append(_candidate('column_influence',(aid,''),target_ref,output['expression'],
                    {**ctx,'influence_role':'row_count'},loc,observation))
    if len(candidates)>1000:
        raise BindingError('lineage_budget','单条语句血缘超过 1000 边，请拆分查询。')
    return dict(candidates=candidates,query_outputs=outputs,context=context)


def parse_sql(content,assets,*,dialect='postgres',observation='design'):
    """Parsing only. No database connections or SQL execution exist in this module."""
    result = dict(status='parsed',parser='sqlglot-'+sqlglot.__version__,statements=[],candidates=[],errors=[])
    try:
        statements = sqlglot.parse(content,read=dialect)
        if len(statements)>30:
            raise BindingError('statement_budget','一次最多解析 30 条语句。')
        for index,statement in enumerate(statements):
            if statement is None:
                continue
            try:
                parsed = parse_statement(statement,assets,dialect,index,observation)
                result['statements'].append({k:v for k,v in parsed.items() if k!='candidates'})
                result['candidates'].extend(parsed['candidates'])
                if len(result['candidates'])>3000:
                    result['candidates'] = result['candidates'][:3000]
                    raise BindingError('candidate_budget','血缘边超过预算，请分批导入。')
            except Exception as exc:
                result['errors'].append(dict(statement_index=index,code=getattr(exc,'code','sql_unresolved'),
                    message=str(exc)[:700]))
    except Exception as exc:
        result['errors'].append(dict(code=getattr(exc,'code','sql_parse_error'),message=str(exc)[:700]))
    if result['errors']:
        result['status'] = 'partial' if result['statements'] else 'unresolved'
    return result
