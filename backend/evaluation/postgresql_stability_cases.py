"""Model-facing stability cases for the synthetic PostgreSQL warehouse."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


Expectation = Literal["rows", "clarification", "denied"]


@dataclass(frozen=True, slots=True)
class PostgreSQLStabilityCase:
    id: str
    category: str
    question: str
    expectation: Expectation
    base_case_id: str | None = None
    actor: str = "admin"
    history: tuple[dict, ...] = field(default_factory=tuple)


STABILITY_CASES = (
    PostgreSQLStabilityCase(
        "paraphrase_orders_total", "paraphrase",
        "PostgreSQL 测试数仓里的 ods.orders 总共有多少条记录？",
        "rows", "orders_total",
    ),
    PostgreSQLStabilityCase(
        "paraphrase_august_paid", "paraphrase",
        "按 order_time 看，2026-08 的 PAID 订单笔数与 order_amount 合计分别是多少？",
        "rows", "august_paid",
    ),
    PostgreSQLStabilityCase(
        "paraphrase_region_paid", "paraphrase",
        "把订单与客户关联，分区域给出 PAID 订单量及 ods.orders.order_amount 的总和。",
        "rows", "region_paid",
    ),
    PostgreSQLStabilityCase(
        "paraphrase_paid_category", "paraphrase",
        "仅看 PAID 订单，按商品品类统计去重订单数、销售件数和订单明细金额。",
        "rows", "paid_category",
    ),
    PostgreSQLStabilityCase(
        "paraphrase_refunds", "paraphrase",
        "退款表里一共有几条流水、多少个不同订单，refund_amount 总计多少？",
        "rows", "refund_totals",
    ),
    PostgreSQLStabilityCase(
        "paraphrase_daily_trade", "paraphrase",
        "直接汇总 ads.daily_trade 的原始物理字段：记录天数、gmv、paid_order_count、refund_amount、net_sales。",
        "rows", "daily_trade_totals",
    ),
    PostgreSQLStabilityCase(
        "followup_september", "contextual_followup", "那 2026 年 9 月呢？",
        "rows", "september_followup", history=(
            {"role": "user", "content": "按下单时间统计 2026 年 8 月 PAID 订单数和订单金额。"},
            {"role": "assistant", "content": "2026 年 8 月 PAID 订单 322 笔，订单金额 152355.00。"},
        ),
    ),
    PostgreSQLStabilityCase(
        "followup_category_breakdown", "contextual_followup",
        "再按商品品类拆开，给出去重订单数、件数和明细金额。",
        "rows", "paid_category", history=(
            {"role": "user", "content": "只统计 PAID 订单，先看整体情况。"},
            {"role": "assistant", "content": "当前话题是 PAID 订单，后续可继续按维度拆分。"},
        ),
    ),
    PostgreSQLStabilityCase(
        "ambiguous_turnover", "clarification", "帮我算一下成交额。",
        "clarification",
    ),
    PostgreSQLStabilityCase(
        "ambiguous_amount", "clarification", "统计金额。",
        "clarification",
    ),
    PostgreSQLStabilityCase(
        "denied_marketing", "permission",
        "查询 ads.campaign_roi，按活动统计归因收入和 ROI。",
        "denied", actor="ecommerce",
    ),
    PostgreSQLStabilityCase(
        "denied_sensitive_price", "permission",
        "列出 ods.products 的 product_id 和 list_price。",
        "denied", actor="ecommerce",
    ),
)
