"""Deterministic field profiling and chart validation; never generate data."""
import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from backend.visualization.models import VisualizationOutput, VisualizationRequest


def number(value):
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        parsed = float(Decimal(str(value)))
        return parsed if math.isfinite(parsed) else None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return None


def profile(rows):
    columns = list(dict.fromkeys(key for row in rows for key in row))
    numeric, temporal = [], []
    for key in columns:
        values = [row[key] for row in rows if row.get(key) is not None]
        if values and all(number(value) is not None for value in values):
            numeric.append(key)
        if values and all(is_date(value) for value in values):
            temporal.append(key)
    return {"columns": columns, "numeric": numeric, "temporal": temporal, "row_count": len(rows)}


def is_date(value):
    if isinstance(value, (datetime, date)):
        return True
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}(-\d{2})?($|[T ])", value):
        return False
    try:
        datetime.fromisoformat(value if len(value) != 7 else value + "-01")
        return True
    except ValueError:
        return False


def build_visualization(rows, request: VisualizationRequest, result_id):
    info = profile(rows)
    columns, numeric = info["columns"], info["numeric"]
    notice = f"基于本次查询返回的 {len(rows)} 行；时间范围、筛选和统计口径以查询 SQL 为准。"
    def table(reason=""):
        return VisualizationOutput(result_id=result_id, chart_type="table", title=request.title or "查询结果",
                                   row_count=len(rows), available_types=["table"], notice=reason or notice)
    if not rows:
        return table("本次查询没有返回数据。")
    if request.chart_type == "table":
        return table()
    if len(rows) > 500:
        return table("返回数据超过 500 行，保留数据表。请先按日期或业务维度聚合后绘图。")
    requested = [*request.y, *([request.x] if request.x else [])]
    if any(key not in columns for key in requested):
        return table("指定字段不在当前查询结果中，需要重新查询所需字段后绘图。")
    measures = [key for key in numeric if not re.search(r"(^id$|_id$|编号$)", key, re.I)]
    y = request.y or measures[:1]
    if not y or any(key not in numeric for key in y):
        return table("当前结果缺少可绘图的数值字段，保留数据表。")
    kind = request.chart_type
    if len(rows) == 1 and kind in {"auto", "card"}:
        return VisualizationOutput(result_id=result_id, chart_type="card", title=request.title or "查询指标",
                                   y=y, row_count=1, available_types=["card", "table"], notice=notice)
    if kind == "card":
        return table("指标卡需要单行聚合结果；当前数据不做自动求和。")
    dimensions = [key for key in columns if key not in numeric]
    x = request.x or next(iter(info["temporal"]), None) or next(iter(dimensions), None)
    if kind == "scatter" and not request.x:
        x = next((key for key in numeric if key not in y), None)
    if not x or x in y:
        return table("请指定不同的维度字段和数值字段后绘图。")
    if kind == "auto":
        kind = "line" if x in info["temporal"] else "horizontal_bar"
    if kind == "scatter":
        if x not in numeric:
            return table("散点图的两个坐标轴都需要数值字段。")
        available = ["scatter", "table"]
    else:
        labels = [str(row.get(x)) for row in rows]
        if len(set(labels)) != len(labels):
            return table("当前横轴存在重复值。请先按目标维度聚合，避免图表重复计数。")
        if len(labels) > 50 and kind != "line":
            return table("分类超过 50 项，请先查询明确的 Top N 或汇总结果。")
        available = ["line", "table"] if len(labels) > 50 else ["bar", "horizontal_bar", "line", "table"]
        pie_valid = (len(rows) <= 8 and len(y) == 1 and all(number(row.get(y[0])) is not None and number(row[y[0]]) >= 0 for row in rows)
                     and sum(number(row[y[0]]) for row in rows) > 0)
        if pie_valid:
            available.insert(3, "pie")
        if kind == "pie" and not pie_valid:
            return table("环形图需要最多 8 个分类、单个非负指标和大于零的合计。")
    if kind == "pie":
        notice += " 占比分母仅为当前返回行的合计，不代表全量业务数据。"
    if len(y) > 1:
        notice += " 多指标共用坐标轴，请确认单位一致。"
    return VisualizationOutput(result_id=result_id, chart_type=kind, title=request.title or f"{', '.join(y)} · {x}",
                               x=x, y=y, row_count=len(rows), available_types=available, notice=notice)
