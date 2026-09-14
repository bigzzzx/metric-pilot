import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import backend.knowledge_base as knowledge_base_module
from backend.knowledge.indexer import KnowledgeDocumentIndexer
from backend.knowledge.models import (
    ChunkDraft,
    DocumentBlock,
    DocumentIngestionError,
    IngestionResult,
    ParsedDocument,
)
from backend.knowledge.repository import (
    claim_processing_document,
    get_knowledge_document,
    init_knowledge_schema,
    list_document_chunks,
    list_knowledge_documents,
    set_document_storage_path,
)
from backend.knowledge.source_storage import MAX_FILE_BYTES, SourceStorage
from backend.rag import prompt_context
from backend.vector_store import VectorStoreError


def _factory(path: Path):
    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    return connect


def _seed_base(connection_factory, identifier="kb-1", owner_id="user-1"):
    init_knowledge_schema(connection_factory)
    connection = connection_factory()
    connection.execute(
        "INSERT INTO knowledge_bases(id,name,description,owner_id) VALUES (?,?,?,?)",
        (identifier, "Test knowledge", "", owner_id),
    )
    connection.commit()
    connection.close()


class _ReadyService:
    def __init__(self, parser_name="test-parser"):
        self.parser_name = parser_name

    def parse_and_chunk(self, filename, media_type, data):
        text = data.decode("utf-8")
        document = ParsedDocument(
            Path(filename).stem,
            Path(filename).suffix.lstrip(".") or "txt",
            (DocumentBlock("paragraph", text, locator={"paragraph": 1}),),
        )
        return IngestionResult(
            document,
            (
                ChunkDraft(
                    content=text,
                    embedding_content=f"文档：{document.title}\n{text}",
                    token_count=len(text),
                    locator={"paragraph": 1},
                    block_types=("paragraph",),
                ),
            ),
            self.parser_name,
        )


def test_schema_migration_preserves_legacy_ready_document(tmp_path):
    factory = _factory(tmp_path / "legacy.db")
    connection = factory()
    connection.executescript(
        """
        CREATE TABLE knowledge_bases(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE knowledge_documents(
            id TEXT PRIMARY KEY, knowledge_base_id TEXT NOT NULL, name TEXT NOT NULL,
            source_type TEXT NOT NULL, chunk_count INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE knowledge_chunks(
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL, chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL, embedding TEXT NOT NULL
        );
        INSERT INTO knowledge_bases(id,name) VALUES ('shared','Shared');
        INSERT INTO knowledge_documents(id,knowledge_base_id,name,source_type,chunk_count)
            VALUES ('doc-1','shared','legacy.md','md',1);
        INSERT INTO knowledge_chunks VALUES ('chunk-1','doc-1',0,'legacy content','[1.0]');
        """
    )
    connection.commit()
    connection.close()

    init_knowledge_schema(factory)

    documents = list_knowledge_documents("shared", "any-user", connection_factory=factory)
    chunks = list_document_chunks("doc-1", "any-user", connection_factory=factory)
    assert documents[0]["status"] == "ready"
    assert documents[0]["warnings"] == []
    assert chunks[0]["content"] == "legacy content"
    assert chunks[0]["embedding_content"] == "legacy content"


def test_owner_isolation_applies_to_reads_and_ingestion(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "owner.db")
    _seed_base(factory)
    indexer = KnowledgeDocumentIndexer(
        connection_factory=factory,
        ingestion_service=_ReadyService(),
        source_storage=SourceStorage(tmp_path / "uploads"),
    )
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "sqlite")

    with pytest.raises(LookupError):
        list_knowledge_documents("kb-1", "user-2", connection_factory=factory)
    with pytest.raises(LookupError):
        indexer.ingest_bytes(
            knowledge_base_id="kb-1",
            owner_id="user-2",
            filename="secret.txt",
            media_type="text/plain",
            data=b"not authorized",
        )
    assert not (tmp_path / "uploads").exists()


def test_source_storage_uses_generated_path_and_enforces_size_limit(tmp_path):
    storage = SourceStorage(tmp_path / "uploads")
    stored = storage.store(
        owner_id="../../user",
        document_id="../../../document",
        filename="../../outside.md",
        data=b"safe content",
    )
    absolute = (storage.root.parent / stored.storage_path).resolve()
    assert storage.root in absolute.parents
    assert absolute.name == "original.md"
    assert absolute.read_bytes() == b"safe content"
    assert ".." not in stored.storage_path

    with pytest.raises(DocumentIngestionError) as error:
        storage.store(
            owner_id="user-1",
            document_id="too-large",
            filename="large.txt",
            data=b"x" * (MAX_FILE_BYTES + 1),
        )
    assert error.value.code == "file_too_large"


def test_sqlite_ingestion_persists_source_chunks_and_structural_metadata(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "ingest.db")
    _seed_base(factory)
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("EMBEDDING_BACKEND", "hash")
    storage = SourceStorage(tmp_path / "uploads")
    indexer = KnowledgeDocumentIndexer(
        connection_factory=factory,
        ingestion_service=_ReadyService(),
        source_storage=storage,
    )

    document = indexer.ingest_bytes(
        knowledge_base_id="kb-1",
        owner_id="user-1",
        filename="metric.md",
        media_type="text/markdown",
        data="# GMV\n\nGMV 是已支付订单金额之和。".encode("utf-8"),
    )

    assert document["status"] == "ready"
    assert document["parser_name"] == "test-parser"
    assert document["chunk_count"] >= 1
    assert document["byte_size"] > 0
    assert len(document["sha256"]) == 64
    assert (storage.root.parent / document["storage_path"]).is_file()
    chunks = list_document_chunks(document["id"], "user-1", connection_factory=factory)
    assert chunks[0]["embedding"]
    assert chunks[0]["embedding_content"]
    assert len(chunks[0]["content_hash"]) == 64
    assert isinstance(chunks[0]["section_path"], list)
    assert isinstance(chunks[0]["locator"], dict)


def test_milvus_failure_compensates_and_marks_document_failed(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "compensate.db")
    _seed_base(factory)
    indexer = KnowledgeDocumentIndexer(
        connection_factory=factory,
        ingestion_service=_ReadyService(),
        source_storage=SourceStorage(tmp_path / "uploads"),
    )
    monkeypatch.setenv("EMBEDDING_BACKEND", "hash")
    monkeypatch.setattr("backend.knowledge.indexer.using_milvus", lambda: True)
    monkeypatch.setattr(
        "backend.knowledge.indexer.upsert_chunks",
        lambda rows: (_ for _ in ()).throw(VectorStoreError("partial upsert")),
    )
    deleted = []
    monkeypatch.setattr("backend.knowledge.indexer.delete_document_chunks", deleted.append)

    with pytest.raises(VectorStoreError):
        indexer.ingest_bytes(
            knowledge_base_id="kb-1",
            owner_id="user-1",
            filename="rules.txt",
            media_type="text/plain",
            data="一条业务规则。".encode("utf-8"),
        )

    documents = list_knowledge_documents("kb-1", "user-1", connection_factory=factory)
    assert documents[0]["status"] == "failed"
    assert documents[0]["error_code"] == "index_unavailable"
    assert documents[0]["storage_path"] == ""
    assert deleted == [documents[0]["id"]]
    assert list_document_chunks(documents[0]["id"], "user-1", connection_factory=factory) == []
    assert not any((tmp_path / "uploads").rglob("original.*"))


def test_ocr_required_is_a_persisted_document_state(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "ocr.db")
    _seed_base(factory)
    storage = SourceStorage(tmp_path / "uploads")

    class OcrService:
        def parse_and_chunk(self, filename, media_type, data):
            raise DocumentIngestionError("ocr_required", "扫描 PDF 需要 OCR。", details={"parser": "pdf"})

    indexer = KnowledgeDocumentIndexer(
        connection_factory=factory,
        ingestion_service=OcrService(),
        source_storage=storage,
    )
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "sqlite")

    with pytest.raises(DocumentIngestionError) as error:
        indexer.ingest_bytes(
            knowledge_base_id="kb-1",
            owner_id="user-1",
            filename="scan.pdf",
            media_type="application/pdf",
            data=b"%PDF scan placeholder",
        )

    document = list_knowledge_documents("kb-1", "user-1", connection_factory=factory)[0]
    assert error.value.details["document_id"] == document["id"]
    assert get_knowledge_document(document["id"], "user-1", connection_factory=factory)["status"] == "ocr_required"
    assert document["error_code"] == "ocr_required"
    assert document["parser_name"] == "pdf"
    assert document["storage_path"]
    assert (storage.root.parent / document["storage_path"]).is_file()


def test_source_write_failure_is_visible_as_failed_document(tmp_path):
    factory = _factory(tmp_path / "storage-failure.db")
    _seed_base(factory)

    class BrokenStorage(SourceStorage):
        def store(self, **kwargs):
            raise OSError("disk unavailable")

    indexer = KnowledgeDocumentIndexer(
        connection_factory=factory,
        ingestion_service=_ReadyService(),
        source_storage=BrokenStorage(tmp_path / "uploads"),
    )
    with pytest.raises(OSError):
        indexer.ingest_bytes(
            knowledge_base_id="kb-1",
            owner_id="user-1",
            filename="rules.txt",
            media_type="text/plain",
            data=b"business rule",
        )

    document = list_knowledge_documents("kb-1", "user-1", connection_factory=factory)[0]
    assert document["status"] == "failed"
    assert document["error_code"] == "storage_failed"
    assert document["storage_path"] == ""


def test_concurrent_identical_uploads_atomically_share_one_document(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "concurrent.db")
    _seed_base(factory)
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("EMBEDDING_BACKEND", "hash")
    barrier = Barrier(2)

    class ConcurrentService(_ReadyService):
        def parse_and_chunk(self, filename, media_type, data):
            result = super().parse_and_chunk(filename, media_type, data)
            barrier.wait(timeout=5)
            return result

    indexer = KnowledgeDocumentIndexer(
        connection_factory=factory,
        ingestion_service=ConcurrentService(),
        source_storage=SourceStorage(tmp_path / "uploads"),
        reconcile_on_startup=False,
    )

    def upload():
        return indexer.ingest_bytes(
            knowledge_base_id="kb-1",
            owner_id="user-1",
            filename="same.txt",
            media_type="text/plain",
            data=b"identical payload",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: upload(), range(2)))

    documents = list_knowledge_documents("kb-1", "user-1", connection_factory=factory)
    assert len(documents) == 1
    assert {result["id"] for result in results} == {documents[0]["id"]}
    assert documents[0]["status"] == "ready"
    assert len(list((tmp_path / "uploads").rglob("original.txt"))) == 1


def test_dedup_key_includes_source_parser_and_index_generation(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "dedup-generation.db")
    _seed_base(factory)
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("EMBEDDING_BACKEND", "hash")
    storage = SourceStorage(tmp_path / "uploads")

    def upload(filename, parser, generation):
        return KnowledgeDocumentIndexer(
            connection_factory=factory,
            ingestion_service=_ReadyService(parser),
            source_storage=storage,
            index_generation=generation,
            reconcile_on_startup=False,
        ).ingest_bytes(
            knowledge_base_id="kb-1",
            owner_id="user-1",
            filename=filename,
            media_type=None,
            data=b"same bytes",
        )

    ids = {
        upload("same.txt", "parser-a", "generation-a")["id"],
        upload("same.md", "parser-a", "generation-a")["id"],
        upload("same.txt", "parser-b", "generation-a")["id"],
        upload("same.txt", "parser-a", "generation-b")["id"],
    }
    assert len(ids) == 4


def test_ocr_source_counts_toward_configured_knowledge_base_quota(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "ocr-quota.db")
    _seed_base(factory)
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "sqlite")

    class OcrService:
        def parse_and_chunk(self, filename, media_type, data):
            raise DocumentIngestionError("ocr_required", "OCR required", details={"parser": "pdf"})

    indexer = KnowledgeDocumentIndexer(
        connection_factory=factory,
        ingestion_service=OcrService(),
        source_storage=SourceStorage(tmp_path / "uploads"),
        owner_quota_bytes=100,
        knowledge_base_quota_bytes=15,
        reconcile_on_startup=False,
    )
    with pytest.raises(DocumentIngestionError) as first:
        indexer.ingest_bytes(
            knowledge_base_id="kb-1",
            owner_id="user-1",
            filename="scan-a.pdf",
            media_type="application/pdf",
            data=b"0123456789",
        )
    assert first.value.code == "ocr_required"

    with pytest.raises(DocumentIngestionError) as second:
        indexer.ingest_bytes(
            knowledge_base_id="kb-1",
            owner_id="user-1",
            filename="scan-b.pdf",
            media_type="application/pdf",
            data=b"abcdefghij",
        )
    assert second.value.code == "storage_quota_exceeded"
    assert len(list_knowledge_documents("kb-1", "user-1", connection_factory=factory)) == 1


def test_owner_quota_is_shared_across_the_owners_knowledge_bases(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "owner-quota.db")
    _seed_base(factory, "kb-1", "user-1")
    _seed_base(factory, "kb-2", "user-1")
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("EMBEDDING_BACKEND", "hash")
    indexer = KnowledgeDocumentIndexer(
        connection_factory=factory,
        ingestion_service=_ReadyService(),
        source_storage=SourceStorage(tmp_path / "uploads"),
        owner_quota_bytes=15,
        knowledge_base_quota_bytes=100,
        reconcile_on_startup=False,
    )
    indexer.ingest_bytes(
        knowledge_base_id="kb-1",
        owner_id="user-1",
        filename="first.txt",
        media_type="text/plain",
        data=b"0123456789",
    )
    with pytest.raises(DocumentIngestionError) as error:
        indexer.ingest_bytes(
            knowledge_base_id="kb-2",
            owner_id="user-1",
            filename="second.txt",
            media_type="text/plain",
            data=b"abcdefghij",
        )
    assert error.value.code == "storage_quota_exceeded"
    assert error.value.details["scope"] == "owner"
    assert list_knowledge_documents("kb-2", "user-1", connection_factory=factory) == []


def test_embedding_batches_never_exceed_hard_limit(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "embedding-batches.db")
    _seed_base(factory)
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "sqlite")

    class ManyChunksService:
        def parse_and_chunk(self, filename, media_type, data):
            document = ParsedDocument("many", "txt", (DocumentBlock("paragraph", "many"),))
            chunks = tuple(
                ChunkDraft(
                    content=f"chunk {index}",
                    embedding_content=f"document many chunk {index}",
                    token_count=4,
                    block_types=("paragraph",),
                )
                for index in range(130)
            )
            return IngestionResult(document, chunks, "many-parser")

    batch_sizes = []

    def embed_batch(texts):
        batch_sizes.append(len(texts))
        return [[1.0] for _ in texts]

    monkeypatch.setattr("backend.knowledge.indexer.embed_texts", embed_batch)
    indexer = KnowledgeDocumentIndexer(
        connection_factory=factory,
        ingestion_service=ManyChunksService(),
        source_storage=SourceStorage(tmp_path / "uploads"),
        embedding_batch_size=10_000,
        reconcile_on_startup=False,
    )
    document = indexer.ingest_bytes(
        knowledge_base_id="kb-1",
        owner_id="user-1",
        filename="many.txt",
        media_type="text/plain",
        data=b"many",
    )
    assert document["chunk_count"] == 130
    assert batch_sizes == [64, 64, 2]


def test_reconciler_cleans_stale_processing_idempotently(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "reconcile.db")
    _seed_base(factory)
    storage = SourceStorage(tmp_path / "uploads")
    document, claimed = claim_processing_document(
        document_id="stale-doc",
        knowledge_base_id="kb-1",
        owner_id="user-1",
        name="stale.txt",
        source_type="txt",
        media_type="text/plain",
        byte_size=5,
        sha256="a" * 64,
        dedup_key="b" * 64,
        parser_name="text",
        index_generation="test",
        owner_quota_bytes=100,
        knowledge_base_quota_bytes=100,
        connection_factory=factory,
    )
    assert claimed
    stored = storage.store(
        owner_id="user-1",
        knowledge_base_id="kb-1",
        document_id=document["id"],
        filename="stale.txt",
        data=b"stale",
    )
    set_document_storage_path(
        document["id"], "user-1", stored.storage_path, connection_factory=factory
    )
    connection = factory()
    connection.execute(
        "UPDATE knowledge_documents SET updated_at='2000-01-01 00:00:00' WHERE id=?",
        (document["id"],),
    )
    connection.commit()
    connection.close()
    deleted = []
    monkeypatch.setattr("backend.knowledge.indexer.using_milvus", lambda: True)
    monkeypatch.setattr("backend.knowledge.indexer.delete_document_chunks", deleted.append)

    indexer = KnowledgeDocumentIndexer(
        connection_factory=factory,
        ingestion_service=_ReadyService(),
        source_storage=storage,
        stale_processing_seconds=1,
        reconcile_on_startup=True,
    )
    recovered = get_knowledge_document(document["id"], "user-1", connection_factory=factory)
    assert recovered["status"] == "failed"
    assert recovered["error_code"] == "stale_processing"
    assert recovered["storage_path"] == ""
    assert not (storage.root.parent / stored.storage_path).exists()
    assert deleted == [document["id"]]
    assert indexer.reconcile_stale_processing() == []
    assert deleted == [document["id"]]


def test_delete_document_removes_sqlite_chunks_vector_and_source(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "delete-document.db")
    _seed_base(factory)
    storage = SourceStorage(tmp_path / "uploads")
    stored = storage.store(
        owner_id="user-1", knowledge_base_id="kb-1", document_id="doc-delete",
        filename="delete.txt", data=b"delete me",
    )
    connection = factory()
    connection.execute(
        """INSERT INTO knowledge_documents(
            id,knowledge_base_id,name,source_type,chunk_count,status,storage_path,updated_at
        ) VALUES (?,?,?,?,1,'ready',?,CURRENT_TIMESTAMP)""",
        ("doc-delete", "kb-1", "delete.txt", "txt", stored.storage_path),
    )
    connection.execute(
        """INSERT INTO knowledge_chunks(
            id,document_id,chunk_index,content,embedding,embedding_content
        ) VALUES ('chunk-delete','doc-delete',0,'delete me','[1.0]','delete me')"""
    )
    connection.commit(); connection.close()
    vector_deletes = []
    monkeypatch.setattr("backend.knowledge.indexer.using_milvus", lambda: True)
    monkeypatch.setattr("backend.knowledge.indexer.delete_document_chunks", vector_deletes.append)
    indexer = KnowledgeDocumentIndexer(
        connection_factory=factory,
        ingestion_service=_ReadyService(),
        source_storage=storage,
        reconcile_on_startup=False,
    )

    deleted = indexer.delete_document(
        knowledge_base_id="kb-1", document_id="doc-delete", owner_id="user-1"
    )

    assert deleted["id"] == "doc-delete"
    assert vector_deletes == ["doc-delete"]
    assert not (storage.root.parent / stored.storage_path).exists()
    connection = factory()
    assert connection.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0] == 0
    connection.close()


def test_all_three_knowledge_retrieval_paths_exclude_non_ready_documents(tmp_path, monkeypatch):
    factory = _factory(tmp_path / "ready-only.db")
    _seed_base(factory)
    connection = factory()
    for status in ("ready", "processing", "failed", "ocr_required"):
        connection.execute(
            """
            INSERT INTO knowledge_documents(
                id,knowledge_base_id,name,source_type,chunk_count,status,updated_at
            ) VALUES (?,?,?,?,1,?,CURRENT_TIMESTAMP)
            """,
            (f"doc-{status}", "kb-1", f"{status}.txt", "txt", status),
        )
        connection.execute(
            """
            INSERT INTO knowledge_chunks(
                id,document_id,chunk_index,content,embedding,
                embedding_content,section_path_json,locator_json
            ) VALUES (?,?,0,?,'[1.0]',?,'[]','{}')
            """,
            (
                f"chunk-{status}",
                f"doc-{status}",
                f"{status} searchable content",
                f"{status} searchable content",
            ),
        )
    connection.commit()
    connection.close()
    monkeypatch.setattr(knowledge_base_module, "db", factory)
    monkeypatch.setattr(knowledge_base_module, "embed_text", lambda text, query=False: [1.0])

    sqlite_results = knowledge_base_module._sqlite_retrieve("searchable", 10, "user-1")
    lexical_results = knowledge_base_module._authorized_chunks("user-1")
    hits = [
        {
            "id": f"chunk-{status}",
            "distance": 1.0,
            "entity": {
                "knowledge_base_id": "kb-1",
                "document_id": f"doc-{status}",
                "kind": "knowledge",
            },
        }
        for status in ("ready", "processing", "failed", "ocr_required")
    ]
    monkeypatch.setattr(knowledge_base_module, "search_chunks", lambda vector, user, limit: hits)
    milvus_results = knowledge_base_module._milvus_retrieve("searchable", 10, "user-1")

    assert [item["id"] for item in sqlite_results] == ["chunk-ready"]
    assert [item["id"] for item in lexical_results] == ["chunk-ready"]
    assert [item["id"] for item in milvus_results] == ["chunk-ready"]


def test_prompt_context_preserves_section_and_file_locator():
    context = prompt_context(
        "GMV",
        [
            {
                "id": "chunk-1",
                "kind": "knowledge",
                "title": "知识库：指标 / rules.pdf",
                "content": "GMV 是已支付订单金额之和。",
                "section_path": ["指标口径", "GMV"],
                "locator": {"page": 3},
            }
        ],
    )

    assert "章节 指标口径 > GMV" in context
    assert "第 3 页" in context
    assert "GMV 是已支付订单金额之和" in context
