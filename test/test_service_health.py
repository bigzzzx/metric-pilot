from backend import service_health


def test_dependency_health_reports_ok_and_preserves_stable_order(monkeypatch):
    monkeypatch.setattr(service_health, "_postgresql", lambda: {"status": "ok"})
    monkeypatch.setattr(service_health, "_vector_store", lambda: {"status": "ok"})
    monkeypatch.setattr(service_health, "_embedding", lambda: {"status": "ok"})
    monkeypatch.setattr(service_health, "_reranker", lambda: {"status": "disabled"})
    monkeypatch.setattr(service_health, "_tool_cache", lambda: {"status": "ok"})
    result = service_health.dependency_health()
    assert result["status"] == "ok"
    assert list(result["dependencies"]) == ["postgresql", "vector_store", "embedding", "reranker", "tool_cache"]
    assert all("latency_ms" in item for item in result["dependencies"].values())


def test_dependency_health_is_sanitized_when_a_probe_raises(monkeypatch):
    def fail():
        raise RuntimeError("password=must-not-leak host=private")
    monkeypatch.setattr(service_health, "_postgresql", fail)
    monkeypatch.setattr(service_health, "_vector_store", lambda: {"status": "ok"})
    monkeypatch.setattr(service_health, "_embedding", lambda: {"status": "ok"})
    monkeypatch.setattr(service_health, "_reranker", lambda: {"status": "ok"})
    monkeypatch.setattr(service_health, "_tool_cache", lambda: {"status": "ok"})
    result = service_health.dependency_health()
    assert result["status"] == "degraded"
    assert result["dependencies"]["postgresql"]["status"] == "unavailable"
    assert "password" not in str(result)
