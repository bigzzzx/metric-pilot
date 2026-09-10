from backend.query_quality.sql_normalization import canonicalize_documented_enums


SCHEMAS = {
    "tables": [{
        "dialect": "postgresql",
        "columns": [
            {"name": "status", "description": "订单状态;PAID=已支付,CANCELLED=已取消,REFUNDED=已退款"},
            {"name": "channel", "description": "订单来源渠道"},
        ],
    }]
}


def test_documented_enum_casing_is_canonicalized_for_equals_and_in():
    sql = canonicalize_documented_enums(
        "SELECT * FROM orders WHERE status='paid' OR status IN ('refunded','CANCELLED')",
        SCHEMAS,
    )
    assert "'PAID'" in sql
    assert "'REFUNDED'" in sql
    assert "'CANCELLED'" in sql


def test_undocumented_string_literals_are_not_changed():
    sql = "SELECT * FROM orders WHERE channel='paid'"
    assert canonicalize_documented_enums(sql, SCHEMAS) == sql
