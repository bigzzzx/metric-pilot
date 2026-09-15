"""Read-only compiler smoke test against the configured synthetic PostgreSQL warehouse."""
import json
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv()
from backend.core import db
from backend.semantic_metrics.models import GovernedMetric,MetricContract,MetricResolution
from backend.semantic_metrics.compiler import compile_metric_query
from backend.tools.sql_tool import run_readonly_sql


def main():
    con=db()
    try:
        asset=con.execute("""SELECT a.*,s.kind FROM catalog_assets a JOIN data_sources s ON s.id=a.source_id
          WHERE a.namespace='ods' AND a.physical_name='orders' AND a.publish_status='published'
          AND s.kind='postgresql' AND s.enabled=1""").fetchone()
        admin=con.execute("SELECT user_id FROM catalog_user_roles WHERE role_id='role-platform-admin' LIMIT 1").fetchone()
        if not asset or not admin: raise RuntimeError('Published synthetic ods.orders and administrator required')
        columns={r[0] for r in con.execute('SELECT physical_name FROM catalog_columns WHERE asset_id=?',(asset['id'],))}
    finally: con.close()
    contract=MetricContract(source_id=asset['source_id'],asset_id=asset['id'],namespace='ods',table='orders',dialect='postgresql',
        measure_expression='SUM(order_amount)',measure_alias='paid_order_amount',time_column='order_time',
        required_filters=[{'column':'status','value':'PAID'}])
    metric=GovernedMetric(id='smoke-only',document_id='smoke-only',version='smoke-only',name='验收金额',display_name='验收金额',
        domain_id=asset['domain_id'],description='PAID 订单的 order_amount 合计，使用 order_time；不是实付金额。',aliases=[],certification_status='certified',contract=contract)
    plan=compile_metric_query('2026年8月验收金额',MetricResolution(status='matched',metrics=[metric]),columns=columns)
    start=time.perf_counter();rows=run_readonly_sql(plan['sql'],admin['user_id'],asset['source_id'])
    elapsed=round((time.perf_counter()-start)*1000,2)
    assert len(rows)==1 and float(rows[0]['paid_order_amount'])==152355.0,rows
    report=dict(status='passed',execution_ms=elapsed,sql=plan['sql'],rows=rows,
                note='Ephemeral compiler contract; no metric or warehouse data was modified')
    target=Path('.data/evals/semantic-postgres.json');target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,default=str))


if __name__=='__main__': main()
