from backend.evaluation.postgresql_cases import CASES, MODEL_CASE_IDS
from backend.evaluation.postgresql_runner import (
    _stability_passed,
    exact_rows_equal,
    model_diagnostics,
    model_rows_equal,
    semantic_rows_equal,
)
from backend.evaluation.postgresql_stability_cases import PostgreSQLStabilityCase, STABILITY_CASES


def test_real_postgresql_suite_has_thirty_unique_governed_cases():
    ids = [case.id for case in CASES]
    assert len(ids) == len(set(ids)) == 30
    assert set(MODEL_CASE_IDS) <= set(ids)
    assert {"metric", "join", "followup", "permission", "availability", "layer_consistency"} <= {
        case.category for case in CASES
    }
    assert all(case.expected for case in CASES if case.outcome == "rows")


def test_direct_scoring_keeps_columns_but_model_scoring_allows_aliases():
    expected = ({"orders": 2, "amount": "10.00"},)
    aliased = [{"订单数": 2, "金额": "10.00"}]
    assert not exact_rows_equal(aliased, expected)
    assert semantic_rows_equal(aliased, expected)
    assert semantic_rows_equal([{"订单数": 2, "订单金额": "10.00", "实付金额": "10.00"}], expected)
    assert not semantic_rows_equal([{"订单数": 3, "金额": "10.00"}], expected)


def test_monthly_model_score_accepts_an_exact_daily_rollup():
    case = next(item for item in CASES if item.id == "august_paid")
    rows = [
        {"day": "2026-08-01", "paid_order_count": 120, "paid_amount": "50000.00"},
        {"day": "2026-08-02", "paid_order_count": 202, "paid_amount": "102355.00"},
    ]
    assert model_rows_equal(case, rows)
    rows[1]["paid_amount"] = "102354.00"
    assert not model_rows_equal(case, rows)


def test_stability_suite_covers_language_context_ambiguity_and_permissions():
    ids = [case.id for case in STABILITY_CASES]
    assert len(ids) == len(set(ids)) == 12
    assert {"paraphrase", "contextual_followup", "clarification", "permission"} == {
        case.category for case in STABILITY_CASES
    }
    assert all(case.base_case_id for case in STABILITY_CASES if case.expectation == "rows")


def test_stability_non_query_outcomes_require_clear_and_safe_answers():
    clarification = PostgreSQLStabilityCase("c", "clarification", "成交额？", "clarification")
    denied = PostgreSQLStabilityCase("d", "permission", "营销？", "denied")
    assert _stability_passed(clarification, {"final": {"answer": "请明确时间范围和成交额口径。"}})[0]
    assert not _stability_passed(clarification, {"sql": "SELECT 1", "final": {"answer": "请明确范围。"}})[0]
    assert _stability_passed(denied, {"permission_error": {"code": "permission_denied"},
                                      "final": {"answer": "你没有该数据资产的访问权限。"}})[0]
    assert not _stability_passed(denied, {"result": [{"secret": 1}],
                                          "final": {"answer": "没有权限。"}})[0]


def test_model_diagnostics_is_bounded_and_keeps_query_evidence():
    state = {
        "route": "data_query", "route_source": "model", "contextual_follow_up": True,
        "rag": [{"id": f"r{i}", "kind": "table", "title": f"t{i}",
                 "asset_ids": [f"a{i}"], "content": "x" * 300} for i in range(20)],
        "verified_schemas": {"tables": [{"asset_id": "a1", "namespace": "ods",
                                           "physical_name": "orders", "source_id": "s1",
                                           "columns": [{"name": f"c{i}"} for i in range(50)]}]},
        "plan": {"sql": "SELECT COUNT(*) FROM ods.orders"},
        "sql": "SELECT COUNT(*) FROM ods.orders", "result": [{"n": 1200}],
        "tool_result": {"status": "success", "data": {"rows": [{"n": 1200}]}, "error": None},
    }
    trace = model_diagnostics(state)
    assert len(trace["retrieved_assets"]) == 12
    assert len(trace["retrieved_assets"][0]["content_excerpt"]) == 180
    assert trace["verified_tables"][0]["table"] == "ods.orders"
    assert len(trace["verified_tables"][0]["columns"]) == 40
    assert trace["tool"]["row_count"] == 1
