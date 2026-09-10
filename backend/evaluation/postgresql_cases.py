"""Versioned golden cases for the synthetic PostgreSQL acceptance warehouse."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PostgreSQLCase:
    id: str
    category: str
    question: str
    sql: str
    expected: tuple[dict, ...] = field(default_factory=tuple)
    outcome: str = "rows"
    actor: str = "admin"
    history: tuple[dict, ...] = field(default_factory=tuple)


CASES = (
    PostgreSQLCase("orders_total", "basic", "PostgreSQL 测试数仓 ods.orders 一共有多少行？",
                   "SELECT COUNT(*) AS n FROM ods.orders", ({"n": 1200},)),
    PostgreSQLCase("order_status_summary", "metric", "按订单状态统计订单数和订单金额。",
                   "SELECT status,COUNT(*) AS n,SUM(order_amount) AS amount FROM ods.orders GROUP BY status ORDER BY status", (
                       {"status": "CANCELLED", "n": 120, "amount": "51600.00"},
                       {"status": "PAID", "n": 960, "amount": "456000.00"},
                       {"status": "REFUNDED", "n": 120, "amount": "56400.00"},
                   )),
    PostgreSQLCase("monthly_orders", "time", "按自然月统计订单数和订单金额。",
                   "SELECT date_trunc('month',order_time)::date AS month,COUNT(*) AS n,SUM(order_amount) AS amount FROM ods.orders GROUP BY 1 ORDER BY 1", (
                       {"month": "2026-07-01", "n": 407, "amount": "191985.00"},
                       {"month": "2026-08-01", "n": 403, "amount": "188865.00"},
                       {"month": "2026-09-01", "n": 390, "amount": "183150.00"},
                   )),
    PostgreSQLCase("august_paid", "time", "按下单时间统计 2026 年 8 月 PAID 订单数和订单金额。",
                   "SELECT COUNT(*) AS n,SUM(order_amount) AS amount FROM ods.orders WHERE status='PAID' AND order_time>='2026-08-01' AND order_time<'2026-09-01'",
                   ({"n": 322, "amount": "152355.00"},)),
    PostgreSQLCase("september_followup", "followup", "换成 2026 年 9 月呢？",
                   "SELECT COUNT(*) AS n,SUM(order_amount) AS amount FROM ods.orders WHERE status='PAID' AND order_time>='2026-09-01' AND order_time<'2026-10-01'",
                   ({"n": 312, "amount": "148650.00"},), history=(
                       {"role": "user", "content": "按下单时间统计 2026 年 8 月 PAID 订单数和订单金额。"},
                       {"role": "assistant", "content": "Tool result: [{\"n\":322,\"amount\":\"152355.00\"}]\nsource_id=b882d48b-8a6e-4ae4-8bfa-a7baeaf05c0d; sql=SELECT COUNT(*) AS n,SUM(order_amount) AS amount FROM ods.orders WHERE status='PAID' AND order_time>='2026-08-01' AND order_time<'2026-09-01'"},
                   )),
    PostgreSQLCase("paid_customers", "metric", "PAID 订单涉及多少位不同客户？",
                   "SELECT COUNT(DISTINCT customer_id) AS n FROM ods.orders WHERE status='PAID'", ({"n": 240},)),
    PostgreSQLCase("channel_summary", "dimension", "按渠道统计订单数和订单金额。",
                   "SELECT channel,COUNT(*) AS n,SUM(order_amount) AS amount FROM ods.orders GROUP BY channel ORDER BY channel", (
                       {"channel": "APP", "n": 400, "amount": "235000.00"},
                       {"channel": "MINI_PROGRAM", "n": 400, "amount": "141000.00"},
                       {"channel": "WEB", "n": 400, "amount": "188000.00"},
                   )),
    PostgreSQLCase("region_paid", "join", "关联客户表，按区域统计 PAID 订单数和订单金额。",
                   "SELECT c.region,COUNT(*) AS n,SUM(o.order_amount) AS amount FROM ods.orders o JOIN ods.customers c ON c.customer_id=o.customer_id WHERE o.status='PAID' GROUP BY c.region ORDER BY c.region", (
                       {"region": "华东", "n": 240, "amount": "112800.00"},
                       {"region": "华北", "n": 240, "amount": "112800.00"},
                       {"region": "华南", "n": 240, "amount": "115200.00"},
                       {"region": "西南", "n": 240, "amount": "115200.00"},
                   )),
    PostgreSQLCase("order_item_totals", "metric", "订单商品明细总行数、商品件数和明细金额是多少？",
                   "SELECT COUNT(*) AS n,SUM(quantity) AS qty,SUM(item_amount) AS amount FROM ods.order_items",
                   ({"n": 2400, "qty": 4800, "amount": "564000.00"},)),
    PostgreSQLCase("paid_category", "join", "只统计 PAID 订单，关联订单明细和商品，按品类统计去重订单数、件数和明细金额。",
                   "SELECT p.category,COUNT(DISTINCT o.order_id) AS orders,SUM(oi.quantity) AS qty,SUM(oi.item_amount) AS amount FROM ods.orders o JOIN ods.order_items oi ON oi.order_id=o.order_id JOIN ods.products p ON p.product_id=oi.product_id WHERE o.status='PAID' GROUP BY p.category ORDER BY p.category", (
                       {"category": "家居", "orders": 480, "qty": 960, "amount": "108000.00"},
                       {"category": "数码", "orders": 480, "qty": 960, "amount": "108000.00"},
                       {"category": "服饰", "orders": 480, "qty": 960, "amount": "120000.00"},
                       {"category": "食品", "orders": 480, "qty": 960, "amount": "120000.00"},
                   )),
    PostgreSQLCase("payment_totals", "metric", "支付流水共有多少条、涉及多少笔订单、支付金额合计多少？",
                   "SELECT COUNT(*) AS n,COUNT(DISTINCT order_id) AS orders,SUM(paid_amount) AS amount FROM ods.payments",
                   ({"n": 1143, "orders": 1080, "amount": "512400.00"},)),
    PostgreSQLCase("multi_payment_orders", "deduplication", "有多少笔订单存在多条支付流水？",
                   "SELECT COUNT(*) AS orders FROM (SELECT order_id FROM ods.payments GROUP BY order_id HAVING COUNT(*)>1) x",
                   ({"orders": 63},)),
    PostgreSQLCase("refund_totals", "metric", "退款流水数、退款订单数和退款金额分别是多少？",
                   "SELECT COUNT(*) AS n,COUNT(DISTINCT order_id) AS orders,SUM(refund_amount) AS amount FROM ods.refunds",
                   ({"n": 120, "orders": 120, "amount": "56400.00"},)),
    PostgreSQLCase("product_category", "dimension", "按商品品类统计商品数和平均标价。",
                   "SELECT category,COUNT(*) AS products,AVG(list_price) AS avg_price FROM ods.products GROUP BY category ORDER BY category", (
                       {"category": "家居", "products": 20, "avg_price": "115.0000000000000000"},
                       {"category": "数码", "products": 20, "avg_price": "110.0000000000000000"},
                       {"category": "服饰", "products": 20, "avg_price": "120.0000000000000000"},
                       {"category": "食品", "products": 20, "avg_price": "125.0000000000000000"},
                   )),
    PostgreSQLCase("latest_inventory", "time", "取最新库存快照日期，按仓库统计可售库存数量。",
                   "SELECT warehouse_code,SUM(available_quantity) AS qty FROM ods.inventory_snapshots WHERE snapshot_date=(SELECT MAX(snapshot_date) FROM ods.inventory_snapshots) GROUP BY warehouse_code ORDER BY warehouse_code", (
                       {"warehouse_code": "WH-EAST", "qty": 4420},
                       {"warehouse_code": "WH-SOUTH", "qty": 4440},
                   )),
    PostgreSQLCase("campaign_events", "marketing", "按营销事件类型统计事件数和事件成本。",
                   "SELECT event_type,COUNT(*) AS n,SUM(event_cost) AS cost FROM ods.campaign_events GROUP BY event_type ORDER BY event_type", (
                       {"event_type": "CLICK", "n": 300, "cost": "240.00"},
                       {"event_type": "CONVERSION", "n": 150, "cost": "375.00"},
                       {"event_type": "IMPRESSION", "n": 1050, "cost": "52.50"},
                   )),
    PostgreSQLCase("campaign_roi", "marketing", "按活动汇总曝光、点击、转化、花费和归因收入。",
                   "SELECT campaign_code,SUM(impressions) AS impressions,SUM(clicks) AS clicks,SUM(conversions) AS conversions,SUM(spend) AS spend,SUM(attributed_revenue) AS revenue FROM ads.campaign_roi GROUP BY campaign_code ORDER BY campaign_code", (
                       {"campaign_code": "CMP-01", "impressions": 100, "clicks": 100, "conversions": 50, "spend": "210.00", "revenue": "6000.00"},
                       {"campaign_code": "CMP-02", "impressions": 250, "clicks": 0, "conversions": 0, "spend": "12.50", "revenue": "0.00"},
                       {"campaign_code": "CMP-03", "impressions": 100, "clicks": 100, "conversions": 50, "spend": "210.00", "revenue": "6000.00"},
                       {"campaign_code": "CMP-04", "impressions": 250, "clicks": 0, "conversions": 0, "spend": "12.50", "revenue": "0.00"},
                       {"campaign_code": "CMP-05", "impressions": 100, "clicks": 100, "conversions": 50, "spend": "210.00", "revenue": "6000.00"},
                       {"campaign_code": "CMP-06", "impressions": 250, "clicks": 0, "conversions": 0, "spend": "12.50", "revenue": "0.00"},
                   )),
    PostgreSQLCase("daily_trade_totals", "aggregate", "汇总交易日报的天数、GMV、有效订单数、退款额和净销售额。",
                   "SELECT COUNT(*) AS days,SUM(gmv) AS gmv,SUM(paid_order_count) AS orders,SUM(refund_amount) AS refunds,SUM(net_sales) AS net FROM ads.daily_trade",
                   ({"days": 92, "gmv": "512400.00", "orders": 1080, "refunds": "56400.00", "net": "456000.00"},)),
    PostgreSQLCase("retention", "aggregate", "列出各统计月和注册月的活跃、留存人数与留存率。",
                   "SELECT stat_month,cohort_month,active_users,retained_users,retention_rate FROM ads.user_retention ORDER BY stat_month,cohort_month", (
                       {"stat_month": "2026-07-01", "cohort_month": "2026-07-01", "active_users": 180, "retained_users": 180, "retention_rate": "1.0000"},
                       {"stat_month": "2026-08-01", "cohort_month": "2026-07-01", "active_users": 165, "retained_users": 118, "retention_rate": "0.7152"},
                       {"stat_month": "2026-08-01", "cohort_month": "2026-08-01", "active_users": 201, "retained_users": 201, "retention_rate": "1.0000"},
                       {"stat_month": "2026-09-01", "cohort_month": "2026-07-01", "active_users": 172, "retained_users": 103, "retention_rate": "0.5988"},
                       {"stat_month": "2026-09-01", "cohort_month": "2026-08-01", "active_users": 210, "retained_users": 137, "retention_rate": "0.6524"},
                       {"stat_month": "2026-09-01", "cohort_month": "2026-09-01", "active_users": 228, "retained_users": 228, "retention_rate": "1.0000"},
                   )),
    PostgreSQLCase("fact_finance", "layer_consistency", "从 DWD 订单事实表汇总 GMV、退款额和净销售额。",
                   "SELECT SUM(paid_amount) AS gmv,SUM(refund_amount) AS refunds,SUM(paid_amount-refund_amount) AS net FROM dwd.fact_order WHERE status IN ('PAID','REFUNDED')",
                   ({"gmv": "512400.00", "refunds": "56400.00", "net": "456000.00"},)),
    PostgreSQLCase("region_day_totals", "layer_consistency", "汇总区域日表的有效订单数和金额。",
                   "SELECT SUM(paid_order_count) AS orders,SUM(paid_amount) AS amount FROM dws.region_day",
                   ({"orders": 1080, "amount": "512400.00"},)),
    PostgreSQLCase("product_day_totals", "layer_consistency", "汇总商品日表的销售件数和金额。",
                   "SELECT SUM(paid_quantity) AS qty,SUM(paid_amount) AS amount FROM dws.product_day",
                   ({"qty": 4320, "amount": "512400.00"},)),
    PostgreSQLCase("user_day_totals", "layer_consistency", "汇总用户日表的有效订单、实付和退款金额。",
                   "SELECT SUM(paid_order_count) AS orders,SUM(paid_amount) AS amount,SUM(refund_amount) AS refunds FROM dws.user_day",
                   ({"orders": 1080, "amount": "512400.00", "refunds": "56400.00"},)),
    PostgreSQLCase("customers_without_orders", "edge", "有多少客户从未下过订单？",
                   "SELECT COUNT(*) AS n FROM ods.customers c WHERE NOT EXISTS (SELECT 1 FROM ods.orders o WHERE o.customer_id=c.customer_id)", ({"n": 0},)),
    PostgreSQLCase("empty_year", "edge", "2025 年有多少笔订单？",
                   "SELECT COUNT(*) AS n FROM ods.orders WHERE order_time>='2025-01-01' AND order_time<'2026-01-01'", ({"n": 0},)),
    PostgreSQLCase("row_limit", "guardrail", "列出全部订单 ID。",
                   "SELECT order_id FROM ods.orders ORDER BY order_id", outcome="row_limit"),
    PostgreSQLCase("sensitive_column", "permission", "列出客户姓名。",
                   "SELECT customer_name FROM ods.customers", outcome="denied", actor="ecommerce"),
    PostgreSQLCase("marketing_denied", "permission", "查询营销活动 ROI。",
                   "SELECT campaign_code,roi FROM ads.campaign_roi", outcome="denied", actor="ecommerce"),
    PostgreSQLCase("supply_denied", "permission", "查询商品标价。",
                   "SELECT product_id,list_price FROM ods.products", outcome="denied", actor="ecommerce"),
    PostgreSQLCase("disabled_source", "availability", "数据源停用后查询订单。",
                   "SELECT COUNT(*) AS n FROM ods.orders", outcome="disabled", actor="admin"),
)


MODEL_CASE_IDS = (
    "orders_total", "august_paid", "september_followup", "region_paid",
    "paid_category", "multi_payment_orders", "refund_totals",
    "latest_inventory", "campaign_events", "daily_trade_totals",
)
