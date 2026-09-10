"""Conservative predicates shared by routing and catalog retrieval."""
from __future__ import annotations

import re


_QUALIFIED_TABLE = re.compile(r"\b[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\b", re.IGNORECASE)
_TECHNICAL_QUERY_MARKERS = (
    "查询", "统计", "汇总", "多少", "几条", "几行", "记录", "字段", "列出",
    "count", "sum", "avg", "min", "max", "select",
)
_CONTEXT_REFERENCE_MARKERS = (
    "这个", "那个", "它", "刚才", "上次", "前面", "继续", "再按", "照旧", "同样",
    "换成", "那 ", "那么",
)
_MUTATION_MARKERS = (
    "insert", "update", "delete", "写入", "插入", "更新", "删除", "修改",
    "执行 etl", "启动 etl", "重跑 etl", "停止 etl",
)
_BUSINESS_METRIC_MARKERS = ("gmv", "复购率", "活跃用户", "转化率", "roi", "客单价", "订单量")
_RAW_FIELD_MARKERS = ("原始字段", "物理字段", "表中现有字段")


def qualified_table_names(message: str) -> set[str]:
    lowered = str(message or "").lower()
    return {match.group(0).lower() for match in _QUALIFIED_TABLE.finditer(lowered)}


def is_unambiguous_technical_query(message: str) -> bool:
    """Detect only self-contained, read-only requests against an explicit table."""

    normalized = " ".join(str(message or "").strip().lower().split())
    if not qualified_table_names(normalized):
        return False
    if any(marker in normalized for marker in _CONTEXT_REFERENCE_MARKERS):
        return False
    if any(marker in normalized for marker in _MUTATION_MARKERS):
        return False
    if not any(marker in normalized for marker in _TECHNICAL_QUERY_MARKERS):
        return False
    if (
        any(marker in normalized for marker in _BUSINESS_METRIC_MARKERS)
        and not any(marker in normalized for marker in _RAW_FIELD_MARKERS)
    ):
        return False
    return True
