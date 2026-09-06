# Local knowledge-base RAG design

## Goal

Let an administrator add business definitions, metric rules, table notes, and
other data documentation from the Agent Extensions page. Added content must be
available to the same RAG retrieval used by the Data Query Agent.

## Scope

The first local version accepts UTF-8 `txt`, `md`, `json`, and `csv` files, as
well as pasted text. The browser reads a selected file and sends its text to the
API, avoiding a multipart upload dependency. PDF and Word parsing are outside
this initial scope.

## Storage and retrieval

SQLite stores knowledge bases, source documents, chunks, and a deterministic
256-dimension hashing embedding for every chunk. This is a real persisted local
vector index with cosine ranking, but not a semantic embedding model. It is a
dependency-free demonstration implementation and can later be replaced behind
the same interface by Qwen/OpenAI embeddings, pgvector, Milvus, or a managed
vector database.

Documents are split into overlapping 600-character chunks. Each chunk retains
the knowledge-base ID, document name, source type, original content, and vector
so results are traceable.

## Runtime flow

`knowledge base -> document -> chunks + vectors -> SQLite -> retrieve(query)`.
At query time, `backend.rag.retrieve()` combines structured table metadata,
relations, existing metric examples, and the most relevant user knowledge-base
chunks. The model receives these chunks as RAG context; raw table rows remain
available only through the guarded SQL tool.

## UI

Agent Extensions becomes a Knowledge Base page. It shows knowledge bases and
document counts, supports creating a base, pasting text, choosing a supported
file, uploading it, and listing ingested documents. It clearly labels the
initial index as “local vector index (hash embedding)” so it does not overclaim
semantic embedding quality.

## Errors and verification

Empty input, unsupported extensions, and oversized files are rejected before
storage. APIs return structured errors. Tests cover chunk persistence and that
a metric definition imported into a knowledge base is returned by retrieval.
