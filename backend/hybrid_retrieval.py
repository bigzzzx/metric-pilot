"""Chinese-aware BM25, Reciprocal Rank Fusion, and optional reranking."""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Iterable

from backend.observability import log_event
from backend.retrieval_models import RetrievalModelError, rerank


def tokenize(text: str) -> list[str]:
    """Tokenize Chinese/English text, preferring jieba search-mode tokens."""
    lowered = text.lower()
    words = re.findall(r"[a-z0-9_]+", lowered)
    chinese = " ".join(re.findall(r"[\u4e00-\u9fff]+", text))
    if chinese:
        try:
            import jieba

            words.extend(token.strip() for token in jieba.cut_for_search(chinese) if token.strip())
        except ImportError:
            for phrase in re.findall(r"[\u4e00-\u9fff]+", chinese):
                words.extend(phrase[index:index + 2] for index in range(max(1, len(phrase) - 1)))
    return words


def bm25_rank(query: str, documents: Iterable[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    rows = list(documents)
    if not rows:
        return []
    tokenized = [tokenize(str(row.get("content", ""))) for row in rows]
    query_terms = tokenize(query)
    if not query_terms:
        return []
    document_frequency = Counter(term for terms in tokenized for term in set(terms))
    average_length = sum(len(terms) for terms in tokenized) / max(1, len(tokenized))
    k1, b = 1.5, 0.75
    ranked: list[tuple[float, dict[str, Any]]] = []
    for row, terms in zip(rows, tokenized):
        frequencies = Counter(terms)
        score = 0.0
        for term in query_terms:
            frequency = frequencies[term]
            if not frequency:
                continue
            count = document_frequency[term]
            inverse_frequency = math.log(1 + (len(rows) - count + 0.5) / (count + 0.5))
            normalizer = frequency + k1 * (1 - b + b * len(terms) / max(average_length, 1))
            score += inverse_frequency * frequency * (k1 + 1) / normalizer
        if score > 0:
            ranked.append((score, {**row, "bm25_score": score}))
    return [row for _, row in sorted(ranked, key=lambda item: item[0], reverse=True)[:limit]]


def reciprocal_rank_fusion(*rankings: list[dict[str, Any]], k: int = 60) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    documents: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            identifier = str(item["id"])
            scores[identifier] = scores.get(identifier, 0.0) + 1.0 / (k + rank)
            documents[identifier] = {**documents.get(identifier, {}), **item}
    return [
        {**documents[identifier], "fusion_score": score}
        for identifier, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)
    ]


def rerank_or_fallback(query: str, candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if not candidates:
        return []
    try:
        ranking = rerank(query, [str(item.get("content", "")) for item in candidates])
    except RetrievalModelError as exc:
        log_event("rag.rerank_fallback", error=str(exc), candidates=len(candidates))
        return candidates[:limit]
    if not ranking or all(score == 0 for _, score in ranking):
        return candidates[:limit]
    return [
        {**candidates[index], "rerank_score": score, "score": round(score, 6)}
        for index, score in ranking[:limit]
        if 0 <= index < len(candidates)
    ]
