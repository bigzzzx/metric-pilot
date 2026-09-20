"""Persisted knowledge base with dense, BM25, RRF, and reranker retrieval."""
import json
import re
import uuid

from backend.core import db
from backend.hybrid_retrieval import (
    bm25_rank,
    exact_identifier_rank,
    reciprocal_rank_fusion,
    rerank_or_fallback,
)
from backend.knowledge.repository import init_knowledge_schema
from backend.observability import log_event
from backend.retrieval_models import RetrievalModelError, embed_text, embed_texts
from backend.vector_store import SHARED_OWNER, VectorStoreError, search_chunks, upsert_chunks, using_milvus

CHUNK_SIZE = 480
CHUNK_OVERLAP = 80
SUPPORTED_EXTENSIONS = {"txt", "md", "json", "csv"}


def _tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.append(phrase)
        tokens.extend(phrase[index:index + 2] for index in range(len(phrase) - 1))
    return tokens


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _json_value(value, fallback):
    if value in (None, ""):
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _chunk_result(row, *, score_key: str, score: float) -> dict:
    """Hydrate retrieval context and source locators from SQLite."""
    return {
        "id": str(row["id"]),
        "kind": "knowledge",
        "title": f"知识库：{row['knowledge_base_name']} / {row['document_name']}",
        "content": row["contextual_content"] or row["content"],
        "keywords": [],
        "section_path": _json_value(row["section_path_json"], []),
        "locator": _json_value(row["locator_json"], {}),
        score_key: round(float(score), 6),
    }


def split_text(text: str, source_type: str = "text") -> list[str]:
    """Structure-aware chunking bounded below BGE's 512-token context window."""
    text = text.strip()
    if not text:
        return []
    if source_type.lower() == "json":
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
    separators = "\n\n|\n(?=#)|\n|(?<=[。！？；.!?;])"
    sections = [section.strip() for section in re.split(separators, text) if section.strip()]
    normalized = "\n".join(sections)
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + CHUNK_SIZE)
        if end < len(normalized):
            boundary = max(normalized.rfind("\n", start, end), normalized.rfind("。", start, end), normalized.rfind(" ", start, end))
            if boundary > start + CHUNK_SIZE // 2:
                end = boundary + 1
        chunks.append(normalized[start:end].strip())
        if end == len(normalized):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return chunks


def list_bases(user_id: str | None = None) -> list[dict]:
    connection = db()
    rows = connection.execute("""
        SELECT kb.id, kb.name, kb.description, kb.created_at, COUNT(doc.id) AS document_count
        FROM knowledge_bases kb LEFT JOIN knowledge_documents doc ON doc.knowledge_base_id = kb.id
        WHERE kb.owner_id IS NULL OR kb.owner_id=?
        GROUP BY kb.id ORDER BY kb.created_at DESC
    """, (user_id,)).fetchall()
    connection.close()
    return [dict(row) for row in rows]


def create_base(name: str, description: str = "", owner_id: str | None = None) -> dict:
    identifier = str(uuid.uuid4())
    connection = db()
    connection.execute("INSERT INTO knowledge_bases(id,name,description,owner_id) VALUES (?,?,?,?)", (identifier, name.strip(), description.strip(), owner_id))
    connection.commit()
    row = connection.execute("SELECT id,name,description,created_at FROM knowledge_bases WHERE id=?", (identifier,)).fetchone()
    connection.close()
    return {**dict(row), "document_count": 0}


def list_documents(knowledge_base_id: str, user_id: str | None = None) -> list[dict]:
    connection = db()
    allowed = connection.execute("SELECT 1 FROM knowledge_bases WHERE id=? AND (owner_id IS NULL OR owner_id=?)", (knowledge_base_id, user_id)).fetchone()
    if not allowed:
        connection.close(); raise LookupError("Knowledge base not found")
    rows = connection.execute("""
        SELECT id, name, source_type, chunk_count, created_at
        FROM knowledge_documents WHERE knowledge_base_id=? ORDER BY created_at DESC
    """, (knowledge_base_id,)).fetchall()
    connection.close()
    return [dict(row) for row in rows]


def add_document(knowledge_base_id: str, name: str, content: str, source_type: str, owner_id: str | None = None) -> dict:
    if not content.strip():
        raise ValueError("Document content cannot be empty")
    if len(content.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("Document is larger than the 2 MB local import limit")
    chunks = split_text(content, source_type)
    if not chunks:
        raise ValueError("Document has no readable text")
    document_id = str(uuid.uuid4())
    connection = db()
    base = connection.execute("SELECT owner_id FROM knowledge_bases WHERE id=? AND (owner_id=? OR (owner_id IS NULL AND ? IS NULL))", (knowledge_base_id, owner_id, owner_id)).fetchone()
    if not base:
        connection.close()
        raise LookupError("Knowledge base not found")
    display_name = name.strip() or "未命名文档"
    try:
        vectors = embed_texts(chunks)
        connection.execute("INSERT INTO knowledge_documents(id,knowledge_base_id,name,source_type,chunk_count) VALUES (?,?,?,?,?)", (document_id, knowledge_base_id, display_name, source_type, len(chunks)))
        vector_rows = []
        for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
            chunk_id = str(uuid.uuid4())
            connection.execute("INSERT INTO knowledge_chunks(id,document_id,chunk_index,content,embedding) VALUES (?,?,?,?,?)", (chunk_id, document_id, index, chunk, json.dumps(vector)))
            vector_rows.append({"id": chunk_id, "owner_id": base["owner_id"] or SHARED_OWNER, "knowledge_base_id": knowledge_base_id, "document_id": document_id, "chunk_index": index, "kind": "knowledge", "title": display_name, "content": chunk, "vector": vector})
        if using_milvus():
            # A document is not committed locally when its external vectors failed.
            upsert_chunks(vector_rows)
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        raise
    row = connection.execute("SELECT id,name,source_type,chunk_count,created_at FROM knowledge_documents WHERE id=?", (document_id,)).fetchone()
    connection.close()
    return dict(row)


def _sqlite_retrieve(query: str, limit: int, user_id: str | None) -> list[dict]:
    """Fallback used by tests and explicit local-only mode."""
    query_vector = embed_text(query, query=True)
    if not any(query_vector):
        return []
    connection = db()
    rows = connection.execute("""
        SELECT chunk.id, chunk.content,
               COALESCE(NULLIF(chunk.embedding_content, ''), chunk.content) AS contextual_content,
               chunk.section_path_json, chunk.locator_json, chunk.embedding,
               doc.name AS document_name, kb.name AS knowledge_base_name
        FROM knowledge_chunks chunk
        JOIN knowledge_documents doc ON doc.id = chunk.document_id
        JOIN knowledge_bases kb ON kb.id = doc.knowledge_base_id
        WHERE doc.status='ready' AND (kb.owner_id IS NULL OR kb.owner_id=?)
    """, (user_id,)).fetchall()
    connection.close()
    ranked = []
    for row in rows:
        score = cosine(query_vector, json.loads(row["embedding"]))
        if score > 0:
            ranked.append((score, row))
    return [
        _chunk_result(row, score_key="score", score=score)
        for score, row in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]
    ]


def _milvus_retrieve(query: str, limit: int, user_id: str | None) -> list[dict]:
    hits = search_chunks(embed_text(query, query=True), user_id, limit)
    if not hits:
        return []
    chunk_ids = [
        str(hit["id"])
        for hit in hits
        if hit["entity"].get("knowledge_base_id") != "__metadata__"
    ]
    labels = {}
    if chunk_ids:
        placeholders = ",".join("?" for _ in chunk_ids)
        connection = db()
        rows = connection.execute(f"""
            SELECT chunk.id, chunk.content,
                   COALESCE(NULLIF(chunk.embedding_content, ''), chunk.content) AS contextual_content,
                   chunk.section_path_json, chunk.locator_json,
                   doc.name AS document_name, kb.name AS knowledge_base_name
            FROM knowledge_chunks chunk
            JOIN knowledge_documents doc ON doc.id=chunk.document_id
            JOIN knowledge_bases kb ON kb.id=doc.knowledge_base_id
            WHERE chunk.id IN ({placeholders}) AND doc.status='ready'
              AND (kb.owner_id IS NULL OR kb.owner_id=?)
        """, [*chunk_ids, user_id]).fetchall()
        connection.close()
        labels = {str(row["id"]): row for row in rows}
    result = []
    for hit in hits:
        entity = hit["entity"]
        if entity.get("knowledge_base_id") == "__metadata__":
            # Catalog metadata has moved to its own collection because its
            # access scope is asset/domain based rather than owner based.
            # Never let legacy shared vectors bypass catalog ACL checks.
            continue
        chunk = labels.get(str(hit["id"]))
        if chunk:
            item = _chunk_result(chunk, score_key="dense_score", score=hit.get("distance", 0))
            item["kind"] = entity.get("kind", "knowledge")
            result.append(item)
    return result


def _authorized_chunks(user_id: str | None) -> list[dict]:
    connection = db()
    rows = connection.execute("""
        SELECT chunk.id, chunk.content,
               COALESCE(NULLIF(chunk.embedding_content, ''), chunk.content) AS contextual_content,
               chunk.section_path_json, chunk.locator_json,
               doc.name AS document_name, kb.name AS knowledge_base_name
        FROM knowledge_chunks chunk
        JOIN knowledge_documents doc ON doc.id = chunk.document_id
        JOIN knowledge_bases kb ON kb.id = doc.knowledge_base_id
        WHERE doc.status='ready' AND (kb.owner_id IS NULL OR kb.owner_id=?)
    """, (user_id,)).fetchall()
    connection.close()
    return [_chunk_result(row, score_key="lexical_source", score=0.0) for row in rows]


def retrieve(query: str, limit: int = 5, user_id: str | None = None, *, apply_rerank: bool = True) -> list[dict]:
    """Hybrid retrieval: dense + Chinese BM25, RRF fusion, then cross-encoder."""
    if not query.strip():
        return []
    init_knowledge_schema()
    candidate_limit = max(limit * 3, 10)
    authorized = _authorized_chunks(user_id)
    lexical = bm25_rank(query, authorized, candidate_limit)
    exact = exact_identifier_rank(query, authorized, candidate_limit)
    # Exact identifiers such as AT-42 are authoritative local lexical hits.
    # Avoid a second remote query embedding when the caller already supplied a
    # boundary-safe identifier present in an authorized chunk.
    if exact:
        fused = reciprocal_rank_fusion(exact, lexical)
        candidates = fused[:candidate_limit]
        return rerank_or_fallback(query, candidates, limit) if apply_rerank else candidates[:limit]
    try:
        if using_milvus():
            dense = _milvus_retrieve(query, candidate_limit, user_id)
        else:
            dense = _sqlite_retrieve(query, candidate_limit, user_id)
    except (RetrievalModelError, VectorStoreError) as exc:
        log_event("rag.dense_fallback", error=str(exc))
        dense = []
    fused = reciprocal_rank_fusion(exact, dense, lexical)
    candidates = fused[:candidate_limit]
    return rerank_or_fallback(query, candidates, limit) if apply_rerank else candidates[:limit]
