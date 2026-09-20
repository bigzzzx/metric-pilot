"""Conservative predicates shared by routing and catalog retrieval."""
from __future__ import annotations

import re


_QUALIFIED_TABLE = re.compile(r"(?<![a-z0-9_])[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*(?![a-z0-9_])", re.IGNORECASE)
_QUALIFIED_COLUMN = re.compile(
    r"(?<![a-z0-9_])[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*(?![a-z0-9_])",
    re.IGNORECASE,
)
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
_RAW_FIELD_MARKERS = (
    "原始字段", "原始物理字段", "物理字段", "物理列", "原始列", "表中现有字段",
    "底表字段", "存储字段", "不套用指标口径", "不使用指标口径",
)


def qualified_table_names(message: str) -> set[str]:
    lowered = str(message or "").lower()
    result = set()
    masked = list(lowered)
    for match in _QUALIFIED_COLUMN.finditer(lowered):
        parts = match.group(0).split(".")
        result.add(".".join(parts[:2]))
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    result.update(match.group(0).lower() for match in _QUALIFIED_TABLE.finditer("".join(masked)))
    return result


def has_explicit_physical_field_intent(message: str) -> bool:
    """Require a qualified physical table before bypassing governed metrics."""

    normalized = " ".join(str(message or "").strip().lower().split())
    if not qualified_table_names(normalized):
        return False
    return bool(_QUALIFIED_COLUMN.search(normalized)) or any(
        marker in normalized for marker in _RAW_FIELD_MARKERS
    )


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
    if any(marker in normalized for marker in _BUSINESS_METRIC_MARKERS) and not has_explicit_physical_field_intent(normalized):
        return False
    return True
