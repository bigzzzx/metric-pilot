"""Golden expected results are explicit, not computed from the SQL under test."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    sql: str = ""
    expected: list[list] = field(default_factory=list)
    outcome: str = "rows"
    history: list[dict] = field(default_factory=list)


CASES = [
    Case("paid_count", "全部已支付订单有多少笔？", "SELECT COUNT(*) AS n FROM orders WHERE status='PAID'", [[6]]),
    Case("gmv", "全部已支付订单 GMV 是多少？", "SELECT SUM(amount) AS gmv FROM orders WHERE status='PAID'", [[490]]),
    Case("august_gmv", "按下单日期 orders.order_date 统计，2026年8月已支付 GMV 是多少？", "SELECT SUM(amount) AS gmv FROM orders WHERE status='PAID' AND order_date>='2026-08-01' AND order_date<'2026-09-01'", [[150]]),
    Case("september", "2026年9月已支付金额是多少？", "SELECT SUM(amount) AS amount FROM orders WHERE status='PAID' AND order_date>='2026-09-01' AND order_date<'2026-10-01'", [[300]]),
    Case("refund", "退款状态订单的金额合计是多少？", "SELECT SUM(amount) AS amount FROM orders WHERE status='REFUNDED'", [[200]]),
    Case("cancelled", "取消状态订单有几笔？", "SELECT COUNT(*) AS n FROM orders WHERE status='CANCELLED'", [[1]]),
    Case("null_amount", "已支付但金额为空的订单有几笔？", "SELECT COUNT(*) AS n FROM orders WHERE status='PAID' AND amount IS NULL", [[1]]),
    Case("zero_amount", "已支付且金额为零的订单有几笔？", "SELECT COUNT(*) AS n FROM orders WHERE status='PAID' AND amount=0", [[1]]),
    Case("buyer_count", "全部已支付订单涉及几个不同用户？", "SELECT COUNT(DISTINCT user_id) AS n FROM orders WHERE status='PAID'", [[3]]),
    Case("east_gmv", "华东用户全部已支付金额是多少？", "SELECT SUM(o.amount) AS amount FROM orders o JOIN users u ON o.user_id=u.user_id WHERE o.status='PAID' AND u.region='华东'", [[150]]),
    Case("category", "全部已支付金额按商品品类汇总。", "SELECT p.category,SUM(o.amount) AS amount FROM orders o JOIN products p ON o.product_id=p.product_id WHERE o.status='PAID' GROUP BY p.category", [["数码",400],["食品",90]]),
    Case("no_orders", "没有任何订单的用户ID有哪些？", "SELECT u.user_id FROM users u LEFT JOIN orders o ON u.user_id=o.user_id WHERE o.order_id IS NULL", [[4]]),
    Case("unknown_region", "区域为空的用户已支付金额是多少？", "SELECT SUM(o.amount) AS amount FROM orders o JOIN users u ON o.user_id=u.user_id WHERE o.status='PAID' AND u.region IS NULL", [[40]]),
    Case("one_to_many", "有成功支付记录的订单金额合计是多少？一个订单只能统计一次。", "SELECT SUM(o.amount) AS amount FROM orders o WHERE EXISTS (SELECT 1 FROM dwd_payment_detail p WHERE p.order_id=o.order_id AND p.payment_status='SUCCESS')", [[150]]),
    Case("payment_amount", "成功支付流水的实付金额合计是多少？", "SELECT SUM(paid_amount) AS amount FROM dwd_payment_detail WHERE payment_status='SUCCESS'", [[150]]),
    Case("empty", "2025年已支付订单有多少笔？", "SELECT COUNT(*) AS n FROM orders WHERE status='PAID' AND order_date>='2025-01-01' AND order_date<'2026-01-01'", [[0]]),
    Case("distinct", "2026年8月有几位用户支付过订单？", "SELECT COUNT(DISTINCT user_id) AS n FROM orders WHERE status='PAID' AND order_date>='2026-08-01' AND order_date<'2026-09-01'", [[3]]),
    Case("followup", "换成2026年9月呢？", "SELECT SUM(amount) AS gmv FROM orders WHERE status='PAID' AND order_date>='2026-09-01' AND order_date<'2026-10-01'", [[300]], history=[{"role":"user","content":"按下单日期 orders.order_date 统计，2026年8月已支付GMV是多少？"},{"role":"assistant","content":"按下单日期统计，2026年8月已支付GMV为150。"}]),
    Case("denied_domain", "查询营销 ROI 看板的所有记录。", "SELECT * FROM ads_campaign_roi", outcome="denied"),
    Case("sensitive", "列出用户姓名。", "SELECT name FROM users", outcome="denied"),
    Case("clarify", "计算留存率。", outcome="clarification"),
    Case("payment_time", "按实际支付发生时间统计2026年8月GMV。", outcome="clarification"),
    Case("greeting", "你好", outcome="chat"),
]
