import sqlite3

import pytest
from fastapi import HTTPException

from backend.evaluation.environment import evaluation_environment
from backend.query_quality.sql_preflight import SQLSchemaError, prepare_readonly
from backend.tools import sql_tool


def test_prepare_never_evaluates_query_function():
    con = sqlite3.connect(":memory:")
    invoked = []
    con.create_function("probe", 0, lambda: invoked.append(True) or 1)
    prepare_readonly(con, "SELECT probe()")
    assert invoked == []
    con.close()


@pytest.mark.parametrize("sql", [
    "SELECT p.pay_date FROM dwd_payment_detail p",
    "SELECT nonexistent FROM orders",
    "SELECT order_id FROM orders o JOIN dwd_payment_detail p ON o.order_id=p.order_id",
])
def test_unknown_or_ambiguous_field_is_terminal_schema_error(tmp_path, sql):
    with evaluation_environment(tmp_path):
        with pytest.raises(SQLSchemaError) as caught:
            sql_tool.run_readonly_sql(sql, "eval-analyst")
        assert caught.value.code == "sql_schema_invalid"
        assert "请确认" in caught.value.detail


def test_existing_permission_checks_still_deny(tmp_path):
    with evaluation_environment(tmp_path):
        for sql in ("SELECT * FROM ads_campaign_roi", "SELECT * FROM users"):
            # users includes sensitive name; SELECT * must remain prohibited.
            with pytest.raises(HTTPException) as caught:
                sql_tool.run_readonly_sql(sql, "eval-analyst")
            assert caught.value.status_code == 403


def test_sqlite_cte_resolves_base_tables(tmp_path):
    with evaluation_environment(tmp_path):
        assert sql_tool.run_readonly_sql('WITH x AS (SELECT order_id FROM orders) SELECT count(*) AS n FROM x', 'eval-analyst')[0]['n'] > 0


def test_missing_field_stops_graph_without_sql_repair(tmp_path):
    from backend import agent
    with evaluation_environment(tmp_path):
        result = agent.query_tool({"plan": {"sql": "SELECT pay_date FROM orders"},
                                   "user_id": "eval-analyst", "route": "data_query"})
        assert result["tool_result"]["error"]["code"] == "sql_schema_invalid"
        assert agent.route_after_query(result) == "query_failed"
        assert "请确认" in agent.failed_query(result)["final"]["answer"]


def test_builtin_preserves_error_classification(monkeypatch):
    from backend.tools import builtins
    from backend.tools.errors import ToolRuntimeError
    from backend.tools.models import ReadonlySqlInput, ToolExecutionContext
    def fail(*args):
        raise SQLSchemaError()
    monkeypatch.setattr(builtins, "run_readonly_sql", fail)
    with pytest.raises(ToolRuntimeError) as caught:
        builtins._readonly_sql(ReadonlySqlInput(sql="SELECT pay_date FROM orders"),
                               ToolExecutionContext(user_id="u", agent_id="data-query-agent", call_id="x"))
    assert caught.value.code == "sql_schema_invalid"
