"""Clients for the isolated embedding and cross-encoder model services."""
from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Any

import httpx

from backend.core import ROOT

EMBEDDING_DIMENSIONS = 512
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class RetrievalModelError(RuntimeError):
    """An embedding or reranking service could not complete a request."""


def setting(name: str, default: str = "") -> str:
    if value := os.getenv(name):
        return value.strip()
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == name:
                return value.strip()
    return default


def _tokens(text: str) -> list[str]:
    tokens = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.append(phrase)
        tokens.extend(phrase[index:index + 2] for index in range(len(phrase) - 1))
    return tokens


def hash_embedding(text: str) -> list[float]:
    """Deterministic test/local fallback; never mixed with the BGE collection."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for token in _tokens(text):
        index = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:4], "big") % EMBEDDING_DIMENSIONS
        vector[index] += 1.0
    magnitude = math.sqrt(sum(value * value for value in vector))
    return [value / magnitude for value in vector] if magnitude else vector


def embed_texts(texts: list[str], *, query: bool = False) -> list[list[float]]:
    if not texts:
        return []
    if setting("EMBEDDING_BACKEND", "hash").lower() == "hash":
        return [hash_embedding(text) for text in texts]

    base_url = setting("EMBEDDING_BASE_URL", "http://127.0.0.1:8081").rstrip("/")
    inputs = [f"{QUERY_INSTRUCTION}{text}" if query else text for text in texts]
    try:
        with httpx.Client(timeout=60, trust_env=False) as client:
            response = client.post(
                f"{base_url}/embed",
                json={"inputs": inputs, "truncate": True, "normalize": True},
            )
            response.raise_for_status()
            payload: Any = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RetrievalModelError(f"Embedding service request failed: {exc}") from exc

    if not isinstance(payload, list) or len(payload) != len(texts):
        raise RetrievalModelError("Embedding service returned an unexpected response")
    vectors = [[float(value) for value in vector] for vector in payload]
    if any(len(vector) != EMBEDDING_DIMENSIONS for vector in vectors):
        raise RetrievalModelError(f"Embedding dimension must be {EMBEDDING_DIMENSIONS}")
    return vectors


def embed_text(text: str, *, query: bool = False) -> list[float]:
    return embed_texts([text], query=query)[0]


def rerank(query: str, documents: list[str]) -> list[tuple[int, float]]:
    """Return (original index, cross-encoder score) in descending order."""
    if not documents:
        return []
    if setting("RERANK_BACKEND", "none").lower() in {"none", "disabled", "off"}:
        return [(index, 0.0) for index in range(len(documents))]

    base_url = setting("RERANK_BASE_URL", "http://127.0.0.1:8082").rstrip("/")
    try:
        with httpx.Client(timeout=90, trust_env=False) as client:
            response = client.post(
                f"{base_url}/rerank",
                json={"query": query, "texts": documents, "truncate": True, "raw_scores": False},
            )
            response.raise_for_status()
            payload: Any = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RetrievalModelError(f"Reranker service request failed: {exc}") from exc

    if not isinstance(payload, list):
        raise RetrievalModelError("Reranker service returned an unexpected response")
    ranked: list[tuple[int, float]] = []
    for item in payload:
        if not isinstance(item, dict) or "index" not in item or "score" not in item:
            raise RetrievalModelError("Reranker result is missing index or score")
        ranked.append((int(item["index"]), float(item["score"])))
    return sorted(ranked, key=lambda item: item[1], reverse=True)
