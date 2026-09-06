"""Re-embed all durable knowledge chunks and publish metadata to Milvus."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core import db  # noqa: E402
from backend.rag import ensure_metadata_index  # noqa: E402
from backend.retrieval_models import embed_texts  # noqa: E402
from backend.vector_store import SHARED_OWNER, upsert_chunks  # noqa: E402


def main() -> None:
    connection = db()
    rows = connection.execute("""
        SELECT chunk.id, chunk.document_id, chunk.chunk_index, chunk.content,
               doc.knowledge_base_id, doc.name, kb.owner_id
        FROM knowledge_chunks chunk
        JOIN knowledge_documents doc ON doc.id=chunk.document_id
        JOIN knowledge_bases kb ON kb.id=doc.knowledge_base_id
        ORDER BY chunk.document_id, chunk.chunk_index
    """).fetchall()
    total = 0
    try:
        for offset in range(0, len(rows), 32):
            batch = rows[offset:offset + 32]
            vectors = embed_texts([row["content"] for row in batch])
            vector_rows = []
            for row, vector in zip(batch, vectors):
                connection.execute("UPDATE knowledge_chunks SET embedding=? WHERE id=?", (json.dumps(vector), row["id"]))
                vector_rows.append({
                    "id": row["id"],
                    "owner_id": row["owner_id"] or SHARED_OWNER,
                    "knowledge_base_id": row["knowledge_base_id"],
                    "document_id": row["document_id"],
                    "chunk_index": row["chunk_index"],
                    "kind": "knowledge",
                    "title": row["name"],
                    "content": row["content"],
                    "vector": vector,
                })
            upsert_chunks(vector_rows)
            total += len(batch)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if not ensure_metadata_index():
        raise RuntimeError("Metadata vectors could not be published")
    print(f"Reindexed {total} knowledge chunks and the authoritative metadata catalog.")


if __name__ == "__main__":
    main()
