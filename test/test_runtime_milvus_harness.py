"""Offline wiring tests for the optional live-Milvus acceptance script.

Only transport/storage and BGE embeddings are fake. Source review, publication,
worker delivery, authorization hydration, withdrawal, and cleanup are real code.
Passing these tests is NOT evidence that a real Milvus service passed acceptance.
"""
from contextlib import ExitStack
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


class Schema:
    def __init__(self):
        self.fields = {}

    def add_field(self, name, kind, **settings):
        self.fields[name] = settings


class Indexes:
    def add_index(self, **settings):
        pass


class FakeMilvus:
    def __init__(self, *, fail_search=False, collision=False):
        self.collections = {'existing_business_collection': {'keep': {'id': 'keep'}}}
        self.created = []
        self.dropped = []
        self.closed = False
        self.fail_search = fail_search
        self.collision = collision
        self.collided_name = None

    def has_collection(self, *, collection_name):
        if self.collision and collection_name.startswith('mp_reliability_acceptance_'):
            self.collision = False
            self.collided_name = collection_name
            self.collections[collection_name] = {'unowned': {'id': 'unowned'}}
        return collection_name in self.collections

    def create_schema(self, **settings):
        return Schema()

    def prepare_index_params(self):
        return Indexes()

    def create_collection(self, *, collection_name, schema, index_params):
        assert collection_name not in self.collections
        assert 'id' in schema.fields and schema.fields['vector']['dim'] == 512
        self.collections[collection_name] = {}
        self.created.append(collection_name)

    def upsert(self, *, collection_name, data):
        for row in data:
            assert len(row['vector']) == 512
            self.collections[collection_name][row['id']] = dict(row)

    @staticmethod
    def matches(row, expression):
        if expression.startswith('id == '):
            return row['id'] == json.loads(expression.removeprefix('id == '))
        if expression.startswith('id in '):
            return row['id'] in json.loads(expression.removeprefix('id in '))
        if expression == 'entity_type != "index_state"':
            return row.get('entity_type') != 'index_state'
        if expression == 'knowledge_base_id == "__metadata__"':
            return row.get('knowledge_base_id') == '__metadata__'
        raise AssertionError('Unexpected query filter: ' + expression)

    def query(self, *, collection_name, filter, output_fields, limit, consistency_level):
        assert consistency_level == 'Strong'
        return [{key: row.get(key) for key in output_fields}
                for row in self.collections[collection_name].values()
                if self.matches(row, filter)][:limit]

    def delete(self, *, collection_name, filter):
        rows = self.collections[collection_name]
        for identifier in [key for key, row in rows.items() if self.matches(row, filter)]:
            del rows[identifier]

    def search(self, *, collection_name, data, filter, limit, output_fields, search_params, consistency_level):
        if self.fail_search:
            raise RuntimeError('injected fake Milvus search failure')
        assert consistency_level == 'Strong'
        result = []
        for vector in data:
            hits = []
            for row in self.collections[collection_name].values():
                if self.matches(row, filter):
                    hits.append({'id': row['id'], 'distance': sum(a * b for a, b in zip(row['vector'], vector)),
                                 'entity': {key: row.get(key) for key in output_fields}})
            result.append(sorted(hits, key=lambda row: row['distance'], reverse=True)[:limit])
        return result

    def drop_collection(self, *, collection_name):
        self.dropped.append(collection_name)
        del self.collections[collection_name]

    def close(self):
        self.closed = True


def run_script(fake):
    from backend import core, checkpoint, vector_store
    from backend.catalog import vector_index
    from backend.retrieval_models import hash_embedding
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_runtime_milvus.py'
    spec = importlib.util.spec_from_file_location('runtime_milvus_acceptance_under_test', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def embeddings(texts, *, query=False):
        calls.append((len(texts), query))
        return [hash_embedding(text) for text in texts]

    class CheckpointHandle:
        closed = False
        def close(self):
            self.closed = True

    checkpoint_handle = CheckpointHandle()
    with ExitStack() as stack:
        # main() is normally a separate process. Restore all process-local
        # settings when invoking it in this pytest process.
        stack.enter_context(patch.dict(os.environ))
        for key in ('DATA_DIR', 'PLATFORM_DB', 'DEMO_DB'):
            stack.enter_context(patch.object(core, key, getattr(core, key)))
        stack.enter_context(patch.object(core.db, '__defaults__', core.db.__defaults__))
        stack.enter_context(patch.object(checkpoint, '_connection', checkpoint_handle))
        stack.enter_context(patch.object(vector_store, 'milvus_client', return_value=fake))
        stack.enter_context(patch.object(vector_index, 'milvus_client', return_value=fake))
        stack.enter_context(patch.object(vector_index, 'embed_texts', embeddings))
        stack.enter_context(patch.object(vector_index, '_indexed_version', ''))
        module.main()
    assert checkpoint_handle.closed
    return calls


def test_runtime_milvus_script_with_fake_transports_preserves_real_governance(capsys):
    fake = FakeMilvus()
    calls = run_script(fake)
    report = json.loads(capsys.readouterr().out)
    assert report['passed'] == report['total'] == 8
    assert all(check['passed'] for check in report['checks'])
    assert calls and any(is_query for _, is_query in calls)
    assert len(fake.created) == len(fake.dropped) == 2
    assert set(fake.created) == set(fake.dropped)
    assert fake.collections == {'existing_business_collection': {'keep': {'id': 'keep'}}}
    assert fake.closed


def test_runtime_milvus_script_failure_cleans_only_owned_collections():
    fake = FakeMilvus(fail_search=True)
    with pytest.raises(AssertionError, match='actual BGE dense retrieval'):
        run_script(fake)
    assert set(fake.created) == set(fake.dropped) and len(fake.created) == 2
    assert 'existing_business_collection' in fake.collections and fake.closed


def test_runtime_milvus_script_collision_does_not_drop_unowned_collection():
    fake = FakeMilvus(collision=True)
    with pytest.raises(RuntimeError, match='refusing reuse'):
        run_script(fake)
    assert fake.collided_name in fake.collections
    assert fake.created == fake.dropped == [] and fake.closed
