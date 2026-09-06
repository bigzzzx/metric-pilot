from backend import memory_retrieval


MEMORIES = [
    {
        "id": "memory-style",
        "user_id": "user-a",
        "memory_type": "preference",
        "memory_key": "response_style",
        "content": "用户偏好：直接说重点",
        "value": "直接说重点",
        "updated_at": "2026-08-30T00:00:00Z",
    },
    {
        "id": "memory-gmv",
        "user_id": "user-a",
        "memory_type": "working_convention",
        "memory_key": "gmv_scope",
        "content": "默认约定：GMV 排除退款订单",
        "value": "GMV 排除退款订单",
        "updated_at": "2026-08-29T00:00:00Z",
    },
]


def test_semantic_memory_result_is_returned_without_keyword_overlap(monkeypatch):
    captured = {}

    def fake_list(user_id, connection_factory=None):
        captured["user_id"] = user_id
        return MEMORIES

    monkeypatch.setattr(memory_retrieval, "list_active_memories", fake_list)
    monkeypatch.setattr(memory_retrieval, "memory_vectors_enabled", lambda: True)
    monkeypatch.setattr(
        memory_retrieval,
        "_milvus_semantic_rank",
        lambda *_args: [{**MEMORIES[0], "vector_score": 0.88}],
    )
    monkeypatch.setattr(
        memory_retrieval,
        "rerank_or_fallback",
        lambda _query, candidates, limit: candidates[:limit],
    )

    results = memory_retrieval.hybrid_search_memories(
        "user-a", "我平时的沟通习惯是什么", limit=3
    )

    assert captured["user_id"] == "user-a"
    assert [item["id"] for item in results] == ["memory-style"]


def test_semantic_failure_degrades_to_lexical_search(monkeypatch):
    monkeypatch.setattr(
        memory_retrieval,
        "list_active_memories",
        lambda *_args, **_kwargs: MEMORIES,
    )
    monkeypatch.setattr(memory_retrieval, "memory_vectors_enabled", lambda: True)
    monkeypatch.setattr(
        memory_retrieval,
        "_milvus_semantic_rank",
        lambda *_args: (_ for _ in ()).throw(ValueError("offline")),
    )
    monkeypatch.setattr(
        memory_retrieval,
        "rerank_or_fallback",
        lambda _query, candidates, limit: candidates[:limit],
    )

    results = memory_retrieval.hybrid_search_memories(
        "user-a", "GMV 退款订单", limit=3
    )

    assert [item["id"] for item in results] == ["memory-gmv"]


def test_memory_retrieval_never_returns_more_than_five(monkeypatch):
    many = [
        {
            **MEMORIES[0],
            "id": f"memory-{index}",
            "content": f"用户偏好：回答方式 {index}",
        }
        for index in range(10)
    ]
    monkeypatch.setattr(
        memory_retrieval,
        "list_active_memories",
        lambda *_args, **_kwargs: many,
    )
    monkeypatch.setattr(memory_retrieval, "memory_vectors_enabled", lambda: True)
    monkeypatch.setattr(
        memory_retrieval,
        "_milvus_semantic_rank",
        lambda *_args: many,
    )
    monkeypatch.setattr(
        memory_retrieval,
        "rerank_or_fallback",
        lambda _query, candidates, limit: candidates[:limit],
    )

    assert len(
        memory_retrieval.hybrid_search_memories(
            "user-a", "回答方式", limit=99
        )
    ) == 5

