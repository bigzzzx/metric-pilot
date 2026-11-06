"""Real Milvus/BGE acceptance in unique collections and throwaway platform storage.

Only synthetic fixtures are transmitted. Availability failure is injected at the
adapter boundary; no live service is stopped or reconfigured.
"""
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch
from contextlib import ExitStack

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    import backend.core as core
    from cryptography.fernet import Fernet
    prefix = 'mp_reliability_acceptance_' + uuid.uuid4().hex
    catalog_name, knowledge_name = prefix + '_catalog', prefix + '_kb'
    os.environ.update(VECTOR_STORE_BACKEND='milvus', EMBEDDING_BACKEND='tei',
        CATALOG_MILVUS_COLLECTION=catalog_name, MILVUS_COLLECTION=knowledge_name,
        CATALOG_INDEX_WORKER_ENABLED='0', MEMORY_VECTOR_BACKEND='disabled', RERANK_BACKEND='none',
        APP_ENCRYPTION_KEY=Fernet.generate_key().decode())
    checks = []
    def check(name, value):
        checks.append(dict(name=name, passed=bool(value)))
        if not value:
            raise AssertionError(name)
    client = None
    created = []
    try:
        with tempfile.TemporaryDirectory(prefix='mp-milvus-reliability-') as directory, ExitStack() as cleanup:
            root = Path(directory)
            core.DATA_DIR, core.PLATFORM_DB, core.DEMO_DB = root, root/'platform.db', root/'warehouse.db'
            core.db.__defaults__ = (core.PLATFORM_DB,)
            from backend.evaluation.data_platform import data_platform_environment
            from backend.evaluation.lineage import prepare_lineage_fixture, GOLDEN_SQL
            from backend.lineage import service
            from backend.lineage.models import EvidenceInput, ReviewInput
            from backend.catalog import vector_index
            from backend import vector_store, index_worker
            from backend.checkpoint import _connection
            cleanup.callback(_connection.close)
            with data_platform_environment(root) as (factory, _, warehouse):
                prepare_lineage_fixture(factory, warehouse)
                client = vector_store.milvus_client()
                for name in (catalog_name, knowledge_name):
                    if client.has_collection(collection_name=name):
                        raise RuntimeError('Unique acceptance collection already exists; refusing reuse')
                # Track only these exact, newly verified names for cleanup.
                created.extend((catalog_name, knowledge_name))
                vector_store._collection(client)
                source = service.import_evidence(EvidenceInput(source_kind='sql_import',
                    title='Synthetic reliability acceptance', content=GOLDEN_SQL,
                    source_id='local-ecommerce-demo'), 'eval-admin')
                candidate = next(c for c in service.list_candidates('eval-admin', source_ref=source['id']) if c['kind']=='join')
                reviewed = service.review_candidate(candidate['id'], ReviewInput(expected_version=1,
                    decision='approve', reason='合成验收：人工批准客户与订单关联'), 'eval-admin')
                published = service.change_candidate_status(candidate['id'], reviewed['version'], 'eval-admin', publish=True)
                job_id = published['index_job_id']
                # Simulate one failed background delivery; recreate consumer below.
                outcome = index_worker.run_job('lineage', job_id, connection_factory=factory,
                    refresh=lambda: False, now=100)
                check('unavailable adapter persists retry instead of success', outcome['status']=='pending')
                index_worker.run_once(connection_factory=factory, refresh=vector_index.ensure_catalog_index, now=200)
                con = factory()
                row = dict(con.execute('SELECT * FROM lineage_index_jobs WHERE id=?', (job_id,)).fetchone())
                con.close()
                check('fresh consumer retries against real Milvus', row['status']=='completed' and row['attempts']==2)
                from backend.catalog.rag_documents import authorized_catalog_documents
                doc = next(d for d in authorized_catalog_documents('eval-admin') if d.get('lineage_candidate_id')==candidate['id'])
                def physical_rows():
                    return client.query(collection_name=catalog_name, filter='id == '+json.dumps(doc['id']),
                        output_fields=['id'], consistency_level='Strong', limit=1)
                check('published candidate is physically indexed', bool(physical_rows()))
                hits = vector_index.retrieve_catalog_vectors('fact_orders 与 customers 通过 customer_id 关联', 'eval-analyst', 200)
                check('actual BGE dense retrieval returns authorized candidate', any(h.get('lineage_candidate_id')==candidate['id'] for h in hits))
                withdrawn = service.change_candidate_status(candidate['id'], 3, 'eval-admin')
                check('withdrawal does not require immediate remote deletion', bool(physical_rows()))
                with patch.object(vector_index, 'ensure_catalog_index', return_value=True):
                    hits = vector_index.retrieve_catalog_vectors('customer_id', 'eval-analyst', 200)
                    check('old real vector cannot bypass current withdrawal', not any(h.get('lineage_candidate_id')==candidate['id'] for h in hits))
                index_worker.run_once(connection_factory=factory, refresh=vector_index.ensure_catalog_index, now=300)
                check('withdrawn physical vector removed by consumer', not physical_rows())
                con = factory()
                con.execute("DELETE FROM catalog_group_memberships WHERE user_id='eval-analyst'")
                con.commit(); con.close()
                check('current permission revocation filters real dense results', vector_index.retrieve_catalog_vectors('fact_orders', 'eval-analyst', 10)==[])
        print(json.dumps(dict(passed=len(checks), total=len(checks), checks=checks,
            mode='real Milvus and BGE; synthetic fixtures; injected adapter outage, not live server outage'), ensure_ascii=False, indent=2))
    finally:
        if client is not None:
            for name in created:
                if not name.startswith(prefix+'_'):
                    raise RuntimeError('Invalid cleanup target')
                if client.has_collection(collection_name=name):
                    client.drop_collection(collection_name=name)
            client.close()


if __name__=='__main__':
    main()
