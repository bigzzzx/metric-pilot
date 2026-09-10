from backend.evaluation.postgresql_cases import CASES, MODEL_CASE_IDS
from backend.evaluation.postgresql_runner import exact_rows_equal, model_rows_equal, semantic_rows_equal


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
