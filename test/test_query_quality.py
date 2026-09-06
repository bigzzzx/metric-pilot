import asyncio
import json

import pytest

from backend.evaluation.environment import evaluation_environment
from backend.evaluation.cases import CASES
from backend.evaluation.runner import offline_case
from backend.evaluation.scoring import rows_equal
from backend.query_quality import evidence, examples
from backend.schemas import QueryPlanOutput


@pytest.fixture
def isolated(tmp_path):
    with evaluation_environment(tmp_path) as factory:
        yield factory


@pytest.mark.parametrize("case", [case for case in CASES if case.sql], ids=lambda case: case.id)
def test_golden_sql_results_and_access(case, isolated):
    assert offline_case(case)["status"] == "passed"


def test_comparison_preserves_duplicates_nulls_and_float_tolerance():
    assert rows_equal([{"x": 0.30000000001}, {"x": None}], [[None], [0.3]])
    assert not rows_equal([{"x": 1}, {"x": 1}], [[1], [2]])


def test_metric_reference_is_required_and_changes_invalidate_plan(isolated):
    metrics = evidence.metric_context("GMV", [], "eval-analyst")
    assert metrics and metrics[0]["required"]
    plan = {"sql": "SELECT SUM(amount) FROM orders WHERE status='PAID'"}
    assert evidence.check_metric_references(plan, metrics, "eval-analyst")
    plan["metric_refs"] = [{"id": metrics[0]["id"], "version": metrics[0]["version"]}]
    assert evidence.check_metric_references(plan, metrics, "eval-analyst") is None
    con = isolated()
    con.execute("UPDATE catalog_metrics SET formula='new definition' WHERE id='metric-gmv'")
    con.commit()
    con.close()
    assert evidence.check_metric_references(plan, metrics, "eval-analyst")


def test_metric_reference_cannot_cite_inaccessible_definition(isolated):
    assert evidence.metric_context("营销 ROI", [], "eval-analyst") == []
    plan = {"sql": "SELECT 1", "metric_refs": [{"id": "catalog:metric:metric-roi", "version": "a" * 64}]}
    assert evidence.check_metric_references(plan, [], "eval-analyst")


def test_shipped_examples_are_verified_and_version_scoped(isolated):
    from backend.tools.sql_tool import run_readonly_sql
    golden = {case.id: case for case in CASES}
    for raw in json.loads(examples.EXAMPLES_FILE.read_text(encoding="utf-8")):
        item = examples.VerifiedExample.model_validate(raw)
        assert rows_equal(run_readonly_sql(item.sql, "eval-analyst"), golden[item.id].expected)
    assert examples.retrieve_examples("已支付订单有多少笔", "eval-analyst")
    assert examples.retrieve_examples("已支付订单", "unknown-user") == []
    con = isolated()
    con.execute("UPDATE catalog_assets SET description='changed grain' WHERE physical_name='orders'")
    con.commit()
    con.close()
    assert examples.retrieve_examples("已支付订单", "eval-analyst") == []


def test_query_planner_receives_business_contract_and_examples(isolated, monkeypatch):
    from backend import agent
    captured = []
    async def complete(_agent, messages, schema, _model):
        captured.extend(messages)
        return schema.model_validate({"answer": "请确认统计时间", "sql": None})
    monkeypatch.setattr(agent, "complete_structured", complete)
    state = {"message": "GMV", "history": [], "user_id": "eval-analyst", "rag": []}
    result = asyncio.run(agent.data_query_agent(state))
    assert "Business contract" in captured[0]["content"]
    assert "metric-gmv" in captured[0]["content"]
    assert result["final"]["answer"] == "请确认统计时间"


def test_invalid_metric_stops_before_sql_executor(isolated, monkeypatch):
    from backend import agent
    def forbidden(*args, **kwargs):
        raise AssertionError("SQL must not execute")
    monkeypatch.setattr(agent.tool_executor, "invoke", forbidden)
    metrics = evidence.metric_context("GMV", [], "eval-analyst")
    state = {"plan": {"sql": "SELECT SUM(amount) FROM orders"}, "user_id": "eval-analyst", "query_metrics": metrics}
    result = agent.query_tool(state)
    assert agent.route_after_query(result) == "query_failed"


def test_broken_optional_examples_degrade_without_blocking_query(isolated, monkeypatch, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(examples, "EXAMPLES_FILE", path)
    assert examples.retrieve_examples("订单", "eval-analyst") == []
