from backend.core import db
from backend.knowledge_base import add_document, create_base, retrieve
from backend.main import init_storage


def _remove_test_bases():
    connection = db()
    ids = [row["id"] for row in connection.execute("SELECT id FROM knowledge_bases WHERE name='pytest-metric-rules'")]
    for identifier in ids:
        connection.execute("DELETE FROM knowledge_chunks WHERE document_id IN (SELECT id FROM knowledge_documents WHERE knowledge_base_id=?)", (identifier,))
        connection.execute("DELETE FROM knowledge_documents WHERE knowledge_base_id=?", (identifier,))
        connection.execute("DELETE FROM knowledge_bases WHERE id=?", (identifier,))
    connection.commit()
    connection.close()


def test_imported_metric_definition_is_retrievable_from_local_vector_index(monkeypatch):
    monkeypatch.setenv("VECTOR_STORE_BACKEND", "sqlite")
    init_storage()
    _remove_test_bases()
    knowledge_base = create_base("pytest-metric-rules", "temporary test knowledge")
    document = add_document(
        knowledge_base["id"],
        "metric-rule.md",
        "净销售额的唯一测试口径：已支付金额减去已退款金额，不包含已取消订单。",
        "file:md",
    )

    results = retrieve("净销售额唯一测试口径", limit=3)

    assert document["chunk_count"] >= 1
    assert any("净销售额" in item["content"] for item in results)

    _remove_test_bases()
