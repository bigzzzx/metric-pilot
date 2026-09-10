from backend.evaluation.postgresql_cases import CASES, MODEL_CASE_IDS
from backend.evaluation.postgresql_runner import exact_rows_equal, semantic_rows_equal


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
    assert not semantic_rows_equal([{"订单数": 3, "金额": "10.00"}], expected)
