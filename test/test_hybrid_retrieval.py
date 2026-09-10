from backend.hybrid_retrieval import bm25_rank, reciprocal_rank_fusion, rerank_or_fallback
from backend.knowledge_base import CHUNK_SIZE, split_text
from backend import rag


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


def test_explicit_postgresql_query_excludes_local_asset_candidates(monkeypatch):
    documents = [
        {"id": "local", "title": "orders", "content": "local", "keywords": [], "asset_ids": ["local-orders"], "source_kind": "local-sqlite"},
        {"id": "remote", "title": "orders", "content": "remote", "keywords": [], "asset_ids": ["pg-orders"], "source_kind": "postgresql"},
        {"id": "metric", "title": "GMV", "content": "口径", "keywords": [], "asset_ids": [], "source_kind": None},
    ]
    monkeypatch.setattr(rag, "authorized_catalog_documents", lambda _user: documents)
    monkeypatch.setattr(rag, "retrieve_catalog_vectors", lambda *_args: documents)
    monkeypatch.setattr(rag, "retrieve_knowledge", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(rag, "rerank_or_fallback", lambda _query, rows, limit: rows[:limit])
    result = rag.retrieve("查询 PostgreSQL 订单", user_id="user")
    assert "local" not in {item["id"] for item in result}
    assert {"remote", "metric"} <= {item["id"] for item in result}


def test_reranker_receives_more_than_the_old_fixed_ten_candidates(monkeypatch):
    documents = [
        {"id": str(index), "title": str(index), "content": "订单 商品", "keywords": [], "asset_ids": [], "source_kind": None}
        for index in range(24)
    ]
    seen = []
    monkeypatch.setattr(rag, "authorized_catalog_documents", lambda _user: documents)
    monkeypatch.setattr(rag, "retrieve_catalog_vectors", lambda *_args: documents)
    monkeypatch.setattr(rag, "retrieve_knowledge", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(rag, "rerank_or_fallback", lambda _query, rows, limit: seen.extend(rows) or rows[:limit])
    rag.retrieve("订单 商品", limit=12, user_id="user")
    assert len(seen) >= 20


def test_explicit_join_keeps_permission_scoped_relationship_evidence(monkeypatch):
    documents = [
        {"id": "orders", "kind": "table", "title": "订单", "content": "订单", "keywords": [], "asset_ids": ["orders"], "source_kind": "postgresql"},
        {"id": "items", "kind": "table", "title": "订单明细", "content": "商品明细", "keywords": [], "asset_ids": ["items"], "source_kind": "postgresql"},
        {"id": "products", "kind": "table", "title": "商品", "content": "商品品类", "keywords": [], "asset_ids": ["products"], "source_kind": "postgresql"},
        {"id": "item-product", "kind": "relation", "title": "明细关联商品", "content": "订单明细关联商品品类", "keywords": ["关联"], "asset_ids": ["items", "products"], "source_kind": "postgresql"},
    ]
    monkeypatch.setattr(rag, "authorized_catalog_documents", lambda _user: documents)
    monkeypatch.setattr(rag, "retrieve_catalog_vectors", lambda *_args: documents[:2])
    monkeypatch.setattr(rag, "retrieve_knowledge", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(rag, "rerank_or_fallback", lambda _query, rows, limit: rows[:limit])
    result = rag.retrieve("PostgreSQL 关联订单明细和商品", limit=3, user_id="user")
    assert result[0]["id"] == "item-product"


def test_implicit_business_breakdown_keeps_relationship_evidence(monkeypatch):
    documents = [
        {"id": "orders", "kind": "table", "title": "订单", "content": "PAID 订单", "keywords": [], "asset_ids": ["orders"], "source_kind": "postgresql"},
        {"id": "items", "kind": "table", "title": "订单明细", "content": "销售件数", "keywords": [], "asset_ids": ["items"], "source_kind": "postgresql"},
        {"id": "products", "kind": "table", "title": "商品", "content": "商品品类", "keywords": [], "asset_ids": ["products"], "source_kind": "postgresql"},
        {"id": "order-items", "kind": "relation", "title": "订单关联明细", "content": "订单关联明细", "keywords": ["关联"], "asset_ids": ["orders", "items"], "source_kind": "postgresql"},
        {"id": "item-product", "kind": "relation", "title": "明细关联商品", "content": "明细关联商品品类", "keywords": ["关联"], "asset_ids": ["items", "products"], "source_kind": "postgresql"},
    ]
    monkeypatch.setattr(rag, "authorized_catalog_documents", lambda _user: documents)
    monkeypatch.setattr(rag, "retrieve_catalog_vectors", lambda *_args: documents[:2])
    monkeypatch.setattr(rag, "retrieve_knowledge", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(rag, "rerank_or_fallback", lambda _query, rows, limit: rows[:limit])
    result = rag.retrieve("仅看 PAID 订单，按商品品类统计销售件数", limit=4, user_id="user")
    assert {item["id"] for item in result[:2]} == {"order-items", "item-product"}


def test_qualified_technical_query_skips_remote_retrieval(monkeypatch):
    documents = [
        {"id": "orders", "kind": "table", "title": "ods.orders", "content": "订单表",
         "keywords": [], "asset_ids": ["orders"], "source_kind": "postgresql"},
        {"id": "customers", "kind": "table", "title": "ods.customers", "content": "客户表",
         "keywords": [], "asset_ids": ["customers"], "source_kind": "postgresql"},
    ]
    monkeypatch.setattr(rag, "authorized_catalog_documents", lambda _user: documents)
    monkeypatch.setattr(
        rag, "retrieve_catalog_vectors",
        lambda *_args: (_ for _ in ()).throw(AssertionError("dense retrieval should be skipped")),
    )
    monkeypatch.setattr(
        rag, "retrieve_knowledge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("knowledge retrieval should be skipped")),
    )
    result = rag.retrieve("查询 PostgreSQL 测试数仓 ods.orders 一共有多少行", user_id="user")
    assert [item["id"] for item in result] == ["orders"]


def test_qualified_business_metric_still_uses_hybrid_retrieval(monkeypatch):
    documents = [
        {"id": "orders", "kind": "table", "title": "ods.orders", "content": "订单表",
         "keywords": [], "asset_ids": ["orders"], "source_kind": "postgresql"},
    ]
    calls = []
    monkeypatch.setattr(rag, "authorized_catalog_documents", lambda _user: documents)
    monkeypatch.setattr(rag, "retrieve_catalog_vectors", lambda *_args: calls.append("dense") or documents)
    monkeypatch.setattr(rag, "retrieve_knowledge", lambda *_args, **_kwargs: calls.append("knowledge") or [])
    monkeypatch.setattr(rag, "rerank_or_fallback", lambda _query, rows, limit: rows[:limit])
    rag.retrieve("查询 ods.orders 的 GMV", user_id="user")
    assert calls == ["dense", "knowledge"]
