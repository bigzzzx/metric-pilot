"""Orchestrates structural grouping, semantic refinement, and token guards."""

from __future__ import annotations

from backend.knowledge.chunking.context import with_previous_sentence
from backend.knowledge.chunking.semantic import EmbeddingClient, RetrievalEmbeddingClient, SemanticSplitter
from backend.knowledge.chunking.structural import PROSE_TYPES, build_segments, combined_locator, split_table_to_limit
from backend.knowledge.chunking.token_budget import RegexTokenCounter, TokenCounter, split_to_token_limit
from backend.knowledge.limits import DEFAULT_LIMITS, IngestionLimits
from backend.knowledge.models import ChunkDraft, ParsedDocument


class ChunkingPipeline:
    def __init__(
        self,
        *,
        token_counter: TokenCounter | None = None,
        embedding_client: EmbeddingClient | None = None,
        target_tokens: int = 320,
        max_tokens: int = 450,
        min_tokens: int = 80,
        limits: IngestionLimits | None = None,
    ) -> None:
        if not 0 < min_tokens <= target_tokens <= max_tokens:
            raise ValueError("Token limits must satisfy 0 < min <= target <= max")
        self.counter = token_counter or RegexTokenCounter()
        self.limits = limits or DEFAULT_LIMITS
        self.embedding_client = embedding_client if embedding_client is not None else RetrievalEmbeddingClient()
        self.target_tokens = target_tokens
        self.max_tokens = max_tokens
        self.min_tokens = min_tokens
        self.semantic = SemanticSplitter(
            token_counter=self.counter,
            embedding_client=self.embedding_client,
            target_tokens=target_tokens,
            max_tokens=max_tokens,
            min_tokens=min_tokens,
            max_sentences=self.limits.max_semantic_sentences,
            embedding_batch_size=self.limits.semantic_embedding_batch_size,
            max_characters_per_token=self.limits.max_characters_per_token_budget,
        )

    def chunk(self, document: ParsedDocument) -> tuple[ChunkDraft, ...]:
        segments = build_segments(
            document,
            counter=self.counter,
            semantic_splitter=self.semantic,
            target_tokens=self.target_tokens,
            max_tokens=self.max_tokens,
            max_characters_per_token=self.limits.max_characters_per_token_budget,
        )
        drafts: list[ChunkDraft] = []
        previous_content: str | None = None
        previous_section: tuple[str, ...] | None = None

        for segment in segments:
            prefix = self._embedding_prefix(document.title, segment.section_path)
            prefix_tokens = self.counter.count(prefix)
            max_prefix_characters = max(8, self.max_tokens // 4) * self.limits.max_characters_per_token_budget
            if prefix_tokens >= self.max_tokens or len(prefix) > max_prefix_characters:
                prefix = self.counter.truncate(prefix, max(8, self.max_tokens // 4))
                prefix = prefix[:max_prefix_characters].rstrip()
                prefix_tokens = self.counter.count(prefix)
            content_budget = max(1, self.max_tokens - prefix_tokens)
            content_character_budget = content_budget * self.limits.max_characters_per_token_budget
            if set(segment.block_types) == {"table"}:
                bounded_parts = split_table_to_limit(
                    segment.content,
                    self.counter,
                    content_budget,
                    max_characters_per_token=self.limits.max_characters_per_token_budget,
                )
            else:
                bounded_parts = split_to_token_limit(
                    segment.content,
                    self.counter,
                    content_budget,
                    max_characters_per_token=self.limits.max_characters_per_token_budget,
                )
            for part_index, part in enumerate(bounded_parts, start=1):
                content = part
                is_prose = bool(set(segment.block_types).issubset(PROSE_TYPES))
                if is_prose and previous_section == segment.section_path:
                    content = with_previous_sentence(
                        content,
                        previous_content,
                        counter=self.counter,
                        max_content_tokens=content_budget,
                    )
                embedding_content = f"{prefix}\n{content}".strip()
                # Custom counters/tokenizer decode behavior can vary, so retain a
                # final invariant guard at the exact embedding payload boundary.
                if self.counter.count(embedding_content) > self.max_tokens or len(content) > content_character_budget:
                    content = self.counter.truncate(content, content_budget)
                    content = content[:content_character_budget].rstrip()
                    embedding_content = f"{prefix}\n{content}".strip()
                locator = combined_locator(segment.locators)
                if len(bounded_parts) > 1:
                    locator.update({"chunk_part": part_index, "chunk_part_count": len(bounded_parts)})
                drafts.append(
                    ChunkDraft(
                        content=content,
                        embedding_content=embedding_content,
                        token_count=self.counter.count(embedding_content),
                        section_path=segment.section_path,
                        locator=locator,
                        block_types=segment.block_types,
                    )
                )
                previous_content = part
                previous_section = segment.section_path
        return tuple(draft for draft in drafts if draft.content)

    @staticmethod
    def _embedding_prefix(title: str, section_path: tuple[str, ...]) -> str:
        if section_path:
            return f"文档：{title}\n章节：{' > '.join(section_path)}"
        return f"文档：{title}"
