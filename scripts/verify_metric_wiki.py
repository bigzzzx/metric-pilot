"""Exercise live-model Wiki draft -> reviewed publication on synthetic storage."""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


async def main():
    from backend.evaluation.environment import evaluation_environment
    from backend.semantic_metrics import wiki
    from backend.catalog.rag_documents import authorized_catalog_documents
    with tempfile.TemporaryDirectory(prefix='metric-wiki-acceptance-') as directory:
        with evaluation_environment(Path(directory)) as factory:
            con=factory();con.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-platform-admin','eval-analyst')");con.commit();con.close()
            start=time.perf_counter()
            draft=await wiki.generate_draft('metric-demo-paid','eval-analyst')
            generation_ms=round((time.perf_counter()-start)*1000,2)
            assert draft['status']=='draft' and draft['content']
            con=factory();row=con.execute("SELECT * FROM catalog_metrics WHERE id='metric-demo-paid'").fetchone();con.close()
            assert json.loads(row['wiki_json'])['origin']=='reviewed-demo'
            review=wiki.MetricReview(expected_version=row['catalog_version'],aliases=json.loads(row['aliases_json']),
                                    contract=json.loads(row['contract_json']),
                                    wiki={k:draft[k] for k in ('summary','content','related_metric_ids','claims')})
            start=time.perf_counter();published=wiki.publish('metric-demo-paid','eval-analyst',review)
            publication_ms=round((time.perf_counter()-start)*1000,2)
            documents=authorized_catalog_documents('eval-analyst')
            assert any(d['kind']=='metric_wiki' and draft['content'] in d['content'] for d in documents)
            report=dict(status='passed',generation_ms=generation_ms,publication_ms=publication_ms,
                        source='isolated synthetic catalog',published=published,draft=draft)
            target=Path('.data/evals/metric-wiki-live.json');target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({k:v for k,v in report.items() if k!='draft'},ensure_ascii=False))


if __name__=='__main__': asyncio.run(main())
