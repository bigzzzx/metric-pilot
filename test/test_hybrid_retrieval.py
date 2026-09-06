from backend.hybrid_retrieval import bm25_rank, reciprocal_rank_fusion, rerank_or_fallback
from backend.knowledge_base import CHUNK_SIZE, split_text


def test_structure_aware_chunking_is_bounded_and_overlapping():
    text = "# 指标口径\n\n" + "GMV 是已支付订单金额。" * 80
    chunks = split_text(text, "md")
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= CHUNK_SIZE for chunk in chunks)
    assert any(set(left[-80:]) & set(right[:80]) for left, right in zip(chunks, chunks[1:]))


def test_chinese_bm25_finds_metric_definition():
    documents = [
        {"id": "gmv", "content": "GMV 是已支付订单金额总和，退款订单需要排除。"},
        {"id": "user", "content": "活跃用户是当天发生登录行为的去重用户。"},
    ]
    ranked = bm25_rank("GMV 怎么计算", documents, 2)
    assert ranked[0]["id"] == "gmv"


def test_rrf_rewards_candidate_recalled_by_both_routes():
    dense = [{"id": "a", "content": "a"}, {"id": "b", "content": "b"}]
    sparse = [{"id": "b", "content": "b"}, {"id": "c", "content": "c"}]
    fused = reciprocal_rank_fusion(dense, sparse)
    assert fused[0]["id"] == "b"


def test_rerank_disabled_preserves_fusion_order(monkeypatch):
    monkeypatch.setenv("RERANK_BACKEND", "none")
    candidates = [{"id": "a", "content": "first"}, {"id": "b", "content": "second"}]
    assert [item["id"] for item in rerank_or_fallback("query", candidates, 2)] == ["a", "b"]
