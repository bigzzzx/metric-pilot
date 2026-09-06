"""Embedding-assisted sentence boundary detection for long prose only."""

from __future__ import annotations

import math
import re
from typing import Protocol

from backend.knowledge.chunking.token_budget import TokenCounter, split_to_token_limit


_SENTENCE = re.compile(r".+?(?:[。！？；]+|[.!?;]+(?=\s|$)|\n+|$)", flags=re.DOTALL)


class EmbeddingClient(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...


class RetrievalEmbeddingClient:
    """Adapter over the project's isolated BGE embedding service client."""

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        from backend.retrieval_models import embed_texts

        return embed_texts(texts)


def split_sentences(text: str) -> list[str]:
    sentences = [match.group(0).strip() for match in _SENTENCE.finditer(text) if match.group(0).strip()]
    return sentences or ([text.strip()] if text.strip() else [])


def cosine_distance(left: list[float], right: list[float]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    similarity = sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
    return max(0.0, min(2.0, 1.0 - similarity))


class SemanticSplitter:
    def __init__(
        self,
        *,
        token_counter: TokenCounter,
        embedding_client: EmbeddingClient | None,
        target_tokens: int,
        max_tokens: int,
        min_tokens: int,
        distance_percentile: float = 0.8,
        max_sentences: int = 1_024,
        embedding_batch_size: int = 64,
        max_characters_per_token: int = 8,
    ) -> None:
        self.counter = token_counter
        self.embedding_client = embedding_client
        self.target_tokens = target_tokens
        self.max_tokens = max_tokens
        self.min_tokens = min_tokens
        self.distance_percentile = distance_percentile
        self.max_sentences = max_sentences
        self.embedding_batch_size = embedding_batch_size
        self.max_characters_per_token = max_characters_per_token

    def split(self, text: str) -> list[str]:
        if (
            self.counter.count(text) <= self.target_tokens
            and len(text) <= self.target_tokens * self.max_characters_per_token
        ):
            return [text.strip()]
        sentences = split_sentences(text)
        if len(sentences) <= 1:
            return split_to_token_limit(
                text,
                self.counter,
                self.max_tokens,
                max_characters_per_token=self.max_characters_per_token,
            )
        if len(sentences) > self.max_sentences:
            return split_to_token_limit(
                text,
                self.counter,
                self.target_tokens,
                max_characters_per_token=self.max_characters_per_token,
            )
        if any(
            self.counter.count(sentence) > self.max_tokens
            or len(sentence) > self.max_tokens * self.max_characters_per_token
            for sentence in sentences
        ):
            return split_to_token_limit(
                text,
                self.counter,
                self.target_tokens,
                max_characters_per_token=self.max_characters_per_token,
            )

        boundaries: set[int] = set()
        if self.embedding_client is not None:
            try:
                vectors = self._embed_batched(sentences)
                if len(vectors) == len(sentences):
                    distances = [cosine_distance(vectors[index], vectors[index + 1]) for index in range(len(vectors) - 1)]
                    threshold = self._threshold(distances)
                    boundaries = {index + 1 for index, distance in enumerate(distances) if distance >= threshold and distance >= 0.15}
            except Exception:
                # Semantic refinement is an optimization. Structural/token safety
                # remains available when the embedding service is temporarily down.
                boundaries = set()

        chunks: list[str] = []
        current: list[str] = []
        for index, sentence in enumerate(sentences):
            candidate = " ".join([*current, sentence]).strip()
            should_split = bool(current) and (
                index in boundaries and self.counter.count(" ".join(current)) >= self.min_tokens
                or self.counter.count(candidate) > self.target_tokens and self.counter.count(" ".join(current)) >= self.min_tokens
            )
            if should_split:
                chunks.append(" ".join(current).strip())
                current = [sentence]
            else:
                current.append(sentence)
        if current:
            chunks.append(" ".join(current).strip())

        bounded: list[str] = []
        for chunk in chunks:
            bounded.extend(
                split_to_token_limit(
                    chunk,
                    self.counter,
                    self.max_tokens,
                    max_characters_per_token=self.max_characters_per_token,
                )
            )
        return bounded

    def _embed_batched(self, sentences: list[str]) -> list[list[float]]:
        assert self.embedding_client is not None
        vectors: list[list[float]] = []
        for start in range(0, len(sentences), self.embedding_batch_size):
            batch = sentences[start:start + self.embedding_batch_size]
            vectors.extend(self.embedding_client.embed_texts(batch))
        return vectors

    def _threshold(self, distances: list[float]) -> float:
        if not distances:
            return 2.0
        ordered = sorted(distances)
        index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * self.distance_percentile) - 1))
        return ordered[index]
