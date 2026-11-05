"""Bounded OpenLineage-style columnLineage import; untrusted external reports."""
import json
from pydantic import Field
from fastapi import HTTPException
from backend.tools.models import StrictModel
from backend.core import db
from backend.lineage import service
from backend.lineage.catalog import assets_for, validate_endpoints, BindingError
from backend.lineage.models import EvidenceInput
from backend.lineage.storage import canonical


class EventInput(StrictModel):
    event: dict
    # keys are namespace + '/' + name, values are actual catalog asset IDs.
    bindings: dict[str,str] = Field(max_length=12)


def import_event(payload: EventInput,user_id,*,connection_factory=None):
    factory = connection_factory or db
    raw = canonical(payload.event)
    if len(raw)>100000: raise HTTPException(413,'外部事件最大 100000 字符。')
    event = payload.event
    state = str(event.get('eventType','')).upper()
    run = event.get('run',{})
    job = event.get('job',{})
    if not isinstance(run,dict) or not isinstance(job,dict):raise HTTPException(422,'run/job 必须是对象。')
    if state not in {'START','RUNNING','COMPLETE','FAIL','ABORT','OTHER'} or not isinstance(run.get('runId'),str) or not isinstance(job.get('name'),str) or not run['runId'] or not job['name']:
        raise HTTPException(422,'需要 eventType、run.runId、job.name；仅支持有界字段血缘事件适配。')
    if len(run['runId'])>128 or len(str(job.get('namespace','')))+len(job['name'])>600:raise HTTPException(422,'任务/运行标识过长。')
    job_identity={k:str(job.get(k,'')) for k in ('namespace','name')}
    con = factory()
    try:
        service.require_admin(con,user_id)
        assets = assets_for(con,user_id,asset_ids=list(payload.bindings.values()))
        if not set(payload.bindings.values())<=set(assets): raise HTTPException(403,'部分绑定资产不存在或无元数据权限。')
        def bound(dataset):
            key = str(dataset.get('namespace',''))+'/'+str(dataset.get('name',''))
            aid = payload.bindings.get(key)
            if aid not in assets: raise BindingError('dataset_unbound','数据集未明确绑定到实际资产：'+key)
            return aid
        candidates,errors = [],[]
        inputs = event.get('inputs',[])
        outputs = event.get('outputs',[])
        if not isinstance(inputs,list) or not isinstance(outputs,list) or any(not isinstance(d,dict) for d in [*inputs,*outputs]):
            raise HTTPException(422,'inputs/outputs 必须是数据集对象数组。')
        if len(inputs)+len(outputs)>30: raise HTTPException(422,'数据集超过预算。')
        input_ids = [bound(item) for item in inputs]
        input_keys = {str(d.get('namespace',''))+'/'+str(d.get('name','')) for d in inputs}
        observation = 'external_reported' if state=='COMPLETE' else 'run_failed' if state in {'FAIL','ABORT'} else 'design'
        for dataset in outputs:
            target = bound(dataset)
            context = dict(all_references=[],source_asset_ids=input_ids,run=dict(run_id=run['runId'],state=state,job=job_identity,event_time=event.get('eventTime')))
            for aid in input_ids:
                candidates.append(dict(kind='table_transform',from_asset_id=aid,to_asset_id=target,
                    from_column='',to_column='',expression='external dataset input/output report',context=context,locator={},observation=observation))
            facets=dataset.get('facets',{})
            if not isinstance(facets,dict) or not isinstance(facets.get('columnLineage',{}),dict):raise HTTPException(422,'facets/columnLineage 必须是对象。')
            fields = facets.get('columnLineage',{}).get('fields',{})
            if not isinstance(fields,dict):raise HTTPException(422,'columnLineage.fields 必须是对象。')
            if len(fields)>200: raise HTTPException(422,'外部字段超过预算。')
            for column,details in fields.items():
                if not isinstance(details,dict) or not isinstance(details.get('inputFields',[]),list):raise HTTPException(422,'字段映射需要 inputFields 数组。')
                references=details.get('inputFields',[])
                if len(references)>100 or any(not isinstance(ref,dict) for ref in references):raise HTTPException(422,'字段引用无效或超过预算。')
                for ref in references:
                    if str(ref.get('namespace',''))+'/'+str(ref.get('name','')) not in input_keys:
                        errors.append(dict(code='non_input_dataset')); continue
                    aid = bound(ref)
                    transforms = ref.get('transformations',[])
                    if not isinstance(transforms,list) or len(transforms)>20 or any(not isinstance(t,dict) for t in transforms):raise HTTPException(422,'transformations 必须是有界对象数组。')
                    indirect = bool(transforms) and all(str(t.get('type','')).upper()=='INDIRECT' for t in transforms)
                    candidate = dict(kind='column_influence' if indirect else 'column_value',from_asset_id=aid,to_asset_id=target,
                        from_column=str(ref.get('field','')),to_column=column,
                        expression=str(details.get('transformationDescription') or details.get('transformationType') or 'external field mapping')[:2000],
                        context={**context,'external_transformations':transforms,'influence_role':'external_indirect' if indirect else None},
                        locator=dict(output_dataset=dataset.get('name'),field=column),observation=observation)
                    try: validate_endpoints(candidate,assets); candidates.append(candidate)
                    except BindingError as exc: errors.append(dict(code=exc.code,message=str(exc)))
        if len(candidates)>1000: raise HTTPException(422,'外部映射超过预算。')
        analysis = dict(status='partial' if errors else 'parsed',candidates=candidates,errors=errors,adapter='openlineage-style-v1')
        evidence = EvidenceInput(title='外部运行 / '+str(job['name'])[:130],content=raw,
            source_uri='external://'+str(job.get('namespace',''))+'/'+str(job['name'])+'/'+str(run['runId']),
            source_version=str(event.get('eventTime') or state)[:128],asset_ids=sorted(set(payload.bindings.values())))
        con.execute('BEGIN IMMEDIATE')
        sid,created = service._store_source(con,evidence,user_id,analysis,source_kind='external_event',
            metadata=dict(run_id=run['runId'],job=job,state=state,bindings=payload.bindings))
        if created: service._persist_candidates(con,sid,analysis,assets)
        con.commit()
        return dict(id=sid,idempotent=not created,analysis={k:v for k,v in analysis.items() if k!='candidates'},
            trust='external_reported_not_internally_observed')
    except BindingError as exc:
        con.rollback(); raise HTTPException(422,dict(code=exc.code,message=str(exc))) from exc
    finally: con.close()
