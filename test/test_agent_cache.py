"""Exact route, query-plan and analysis-answer cache behavior."""
import asyncio
import json

from backend import agent
from backend.agent_cache import RedisAgentCache
from backend.evaluation.environment import evaluation_environment
from backend.schemas import AnalysisOutput, QueryPlanOutput, RouteOutput


class DictAgentCache:
    def __init__(self):
        self.values = {}

    def _key(self, namespace, identity):
        return namespace, json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)

    def get(self, namespace, identity):
        return self.values.get(self._key(namespace, identity))

    def set(self, namespace, identity, payload, _ttl_seconds):
        self.values[self._key(namespace, identity)] = payload


class BoundedRedis:
    def __init__(self):
        self.values = {}
        self.scores = {}

    def set(self, key, value, ex=None):
        self.values[key] = value

    def get(self, key):
        return self.values.get(key)

    def zremrangebyscore(self, _index, _minimum, maximum):
        self.scores = {key: score for key, score in self.scores.items() if score > float(maximum)}

    def zadd(self, _index, values):
        self.scores.update(values)

    def zcard(self, _index):
        return len(self.scores)

    def zpopmin(self, _index, count):
        victims = sorted(self.scores.items(), key=lambda item: item[1])[:count]
        for key, _score in victims:
            self.scores.pop(key, None)
        return victims

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)


def test_agent_cache_is_bounded_in_addition_to_ttl():
    redis = BoundedRedis()
    cache = RedisAgentCache(redis, prefix="bounded", max_entries=2)

    for index in range(3):
        cache.set("turn-response", {"question": index}, {"answer": str(index)}, 300)

    assert len(redis.values) == 2
    assert len(redis.scores) == 2


def test_query_plan_cache_skips_repeated_planner_model_call(tmp_path, monkeypatch):
    cache = DictAgentCache()
    calls = []

    async def complete(_agent_id, _messages, schema, _model_id):
        calls.append(schema)
        return QueryPlanOutput(answer="当前权限下没有需要执行的查询", sql=None)

    with evaluation_environment(tmp_path):
        monkeypatch.setattr(agent, "agent_cache", cache)
        monkeypatch.setattr(agent, "complete_structured", complete)
        state = {"message": "说明可用数据", "history": [], "user_id": "eval-analyst", "rag": []}
        first = asyncio.run(agent.data_query_agent(state))
        second = asyncio.run(agent.data_query_agent(state))

    assert calls == [QueryPlanOutput]
    assert first["plan_cache_hit"] is False
    assert second["plan_cache_hit"] is True


def test_analysis_answer_cache_skips_repeated_analysis_model_call(monkeypatch):
    cache = DictAgentCache()
    calls = []

    async def complete(_agent_id, _messages, schema, _model_id):
        calls.append(schema)
        return AnalysisOutput(answer="华东高于华南。", summary="华东更高")

    monkeypatch.setattr(agent, "agent_cache", cache)
    monkeypatch.setattr(agent, "complete_structured", complete)
    state = {
        "message": "分析区域差异", "history": [], "user_id": "user-1", "rag": [],
        "sql": "SELECT region, COUNT(*) AS n FROM orders GROUP BY region",
        "result": [{"region": "华东", "n": 8}, {"region": "华南", "n": 3}],
    }
    first = asyncio.run(agent.data_analysis_agent(state))
    second = asyncio.run(agent.data_analysis_agent(state))

    assert calls == [AnalysisOutput]
    assert first["answer_cache_hit"] is False
    assert second["answer_cache_hit"] is True
    assert second["final"]["answer"] == "华东高于华南。"


def test_router_cache_skips_repeated_router_model_call(monkeypatch):
    cache = DictAgentCache()
    calls = []

    async def complete(_agent_id, _messages, schema, _model_id):
        calls.append(schema)
        return RouteOutput(route="general")

    monkeypatch.setattr(agent, "agent_cache", cache)
    monkeypatch.setattr(agent, "complete_structured", complete)
    monkeypatch.setattr(agent, "has_tool", lambda *_args: False)
    monkeypatch.setattr(agent, "bound_skill_summaries", lambda: [])
    monkeypatch.setattr(agent, "tool_catalog_for_router", lambda: [])
    state = {
        "message": "请解释客户复购率这个概念", "history": [], "user_id": "user-1",
        "last_query_result": None,
    }
    first = asyncio.run(agent.router(state))
    second = asyncio.run(agent.router(state))

    assert calls == [RouteOutput]
    assert first["route_cache_hit"] is False
    assert second["route_cache_hit"] is True
