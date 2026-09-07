\set ON_ERROR_STOP on

INSERT INTO ods.customers
SELECT g,
       '合成客户' || lpad(g::text, 4, '0'),
       '138' || lpad(g::text, 8, '0'),
       'user' || g || '@synthetic.example',
       (ARRAY['华东','华南','华北','西南'])[(g % 4) + 1],
       (ARRAY['上海','杭州','深圳','广州','北京','成都'])[(g % 6) + 1],
       timestamp '2026-01-01' + (g % 180) * interval '1 day',
       'synthetic_crm'
FROM generate_series(1, 300) AS g;

INSERT INTO ods.products
SELECT g,
       '合成商品' || lpad(g::text, 3, '0'),
       (ARRAY['数码','家居','服饰','食品'])[(g % 4) + 1],
       (g % 12) + 1,
       (20 + (g % 40) * 5)::numeric(12,2),
       timestamp '2025-12-01' + (g % 60) * interval '1 day'
FROM generate_series(1, 80) AS g;

INSERT INTO ods.orders (order_id, customer_id, order_time, status, channel)
SELECT g,
       ((g * 7) % 300) + 1,
       timestamp '2026-07-01 08:00:00' + (g % 92) * interval '1 day' + (g % 12) * interval '1 hour',
       CASE WHEN g % 10 = 0 THEN 'CANCELLED' WHEN g % 10 = 1 THEN 'REFUNDED' ELSE 'PAID' END,
       (ARRAY['APP','WEB','MINI_PROGRAM'])[(g % 3) + 1]
FROM generate_series(1, 1200) AS g;

INSERT INTO ods.order_items
SELECT (o.order_id - 1) * 2 + item_no,
       o.order_id,
       ((o.order_id * 7 + item_no * 11) % 80) + 1,
       ((o.order_id + item_no) % 3) + 1,
       p.list_price,
       (((o.order_id + item_no) % 3) + 1) * p.list_price
FROM ods.orders o
CROSS JOIN generate_series(1, 2) AS item_no
JOIN ods.products p ON p.product_id = ((o.order_id * 7 + item_no * 11) % 80) + 1;

UPDATE ods.orders o
SET order_amount = totals.amount
FROM (
  SELECT order_id, sum(item_amount) AS amount
  FROM ods.order_items
  GROUP BY order_id
) totals
WHERE totals.order_id = o.order_id;

INSERT INTO ods.payments
SELECT o.order_id * 10 + 1,
       o.order_id,
       o.order_time + interval '15 minutes',
       (ARRAY['ALIPAY','WECHAT','CARD'])[(o.order_id % 3) + 1],
       CASE WHEN o.order_id % 17 = 0 THEN round(o.order_amount * 0.60, 2) ELSE o.order_amount END
FROM ods.orders o
WHERE o.status IN ('PAID','REFUNDED');

INSERT INTO ods.payments
SELECT o.order_id * 10 + 2,
       o.order_id,
       o.order_time + interval '25 minutes',
       'CARD',
       o.order_amount - round(o.order_amount * 0.60, 2)
FROM ods.orders o
WHERE o.status IN ('PAID','REFUNDED') AND o.order_id % 17 = 0;

INSERT INTO ods.refunds
SELECT o.order_id,
       o.order_id,
       o.order_time + interval '3 days',
       CASE WHEN o.order_id % 2 = 0 THEN 'CUSTOMER_REQUEST' ELSE 'QUALITY_ISSUE' END,
       CASE WHEN o.order_id % 2 = 0 THEN round(o.order_amount * 0.50, 2) ELSE o.order_amount END
FROM ods.orders o
WHERE o.status = 'REFUNDED';

INSERT INTO ods.inventory_snapshots
SELECT date '2026-09-01' + snapshot_no * 7,
       p.product_id,
       (ARRAY['WH-EAST','WH-SOUTH'])[(p.product_id % 2) + 1],
       20 + ((p.product_id * 13 + snapshot_no * 7) % 180)
FROM ods.products p
CROSS JOIN generate_series(0, 3) AS snapshot_no;

INSERT INTO ods.campaign_events
SELECT g,
       timestamp '2026-07-01' + (g % 92) * interval '1 day' + (g % 24) * interval '1 hour',
       'CMP-' || lpad(((g % 6) + 1)::text, 2, '0'),
       ((g * 11) % 300) + 1,
       CASE WHEN g % 10 = 0 THEN 'CONVERSION' WHEN g % 4 = 0 THEN 'CLICK' ELSE 'IMPRESSION' END,
       CASE WHEN g % 10 = 0 THEN 2.50 WHEN g % 4 = 0 THEN 0.80 ELSE 0.05 END
FROM generate_series(1, 1500) AS g;

INSERT INTO dwd.dim_customer
SELECT customer_id, customer_name, region, city, registered_at::date
FROM ods.customers;

INSERT INTO dwd.dim_product
SELECT product_id, product_name, category, supplier_id, list_price
FROM ods.products;

INSERT INTO dwd.fact_order
SELECT o.order_id,
       o.customer_id,
       o.order_time::date,
       o.status,
       o.order_amount,
       coalesce(p.paid_amount, 0),
       coalesce(r.refund_amount, 0),
       o.channel
FROM ods.orders o
LEFT JOIN (SELECT order_id, sum(paid_amount) AS paid_amount FROM ods.payments GROUP BY order_id) p USING (order_id)
LEFT JOIN (SELECT order_id, sum(refund_amount) AS refund_amount FROM ods.refunds GROUP BY order_id) r USING (order_id);

INSERT INTO dwd.fact_order_item SELECT * FROM ods.order_items;
INSERT INTO dwd.fact_payment
SELECT payment_id, order_id, payment_time::date, payment_method, paid_amount FROM ods.payments;
INSERT INTO dwd.fact_campaign_event
SELECT event_id, event_time::date, campaign_code, customer_id, event_type, event_cost FROM ods.campaign_events;

INSERT INTO dws.user_day
SELECT order_date,
       customer_id,
       count(*) FILTER (WHERE status IN ('PAID','REFUNDED')),
       coalesce(sum(paid_amount) FILTER (WHERE status IN ('PAID','REFUNDED')), 0),
       coalesce(sum(refund_amount), 0)
FROM dwd.fact_order
GROUP BY order_date, customer_id;

INSERT INTO dws.product_day
SELECT o.order_date,
       i.product_id,
       coalesce(sum(i.quantity) FILTER (WHERE o.status IN ('PAID','REFUNDED')), 0),
       coalesce(sum(i.item_amount) FILTER (WHERE o.status IN ('PAID','REFUNDED')), 0)
FROM dwd.fact_order_item i
JOIN dwd.fact_order o USING (order_id)
GROUP BY o.order_date, i.product_id;

INSERT INTO dws.region_day
SELECT o.order_date,
       c.region,
       count(DISTINCT o.customer_id) FILTER (WHERE o.status IN ('PAID','REFUNDED')),
       count(*) FILTER (WHERE o.status IN ('PAID','REFUNDED')),
       coalesce(sum(o.paid_amount) FILTER (WHERE o.status IN ('PAID','REFUNDED')), 0)
FROM dwd.fact_order o
JOIN dwd.dim_customer c USING (customer_id)
GROUP BY o.order_date, c.region;

INSERT INTO ads.daily_trade
SELECT order_date,
       sum(paid_amount) FILTER (WHERE status IN ('PAID','REFUNDED')) AS gmv,
       count(*) FILTER (WHERE status IN ('PAID','REFUNDED')) AS paid_order_count,
       count(DISTINCT customer_id) FILTER (WHERE status IN ('PAID','REFUNDED')) AS paid_user_count,
       sum(refund_amount) AS refund_amount,
       sum(paid_amount) FILTER (WHERE status IN ('PAID','REFUNDED')) - sum(refund_amount) AS net_sales
FROM dwd.fact_order
GROUP BY order_date;

INSERT INTO ads.user_retention VALUES
  ('2026-07-01','2026-07-01',180,180,1.0000),
  ('2026-08-01','2026-07-01',165,118,0.7152),
  ('2026-08-01','2026-08-01',201,201,1.0000),
  ('2026-09-01','2026-07-01',172,103,0.5988),
  ('2026-09-01','2026-08-01',210,137,0.6524),
  ('2026-09-01','2026-09-01',228,228,1.0000);

INSERT INTO ads.campaign_roi
SELECT event_date,
       campaign_code,
       count(*) FILTER (WHERE event_type = 'IMPRESSION'),
       count(*) FILTER (WHERE event_type = 'CLICK'),
       count(*) FILTER (WHERE event_type = 'CONVERSION'),
       sum(event_cost),
       count(*) FILTER (WHERE event_type = 'CONVERSION') * 120.00,
       CASE WHEN sum(event_cost) = 0 THEN NULL
            ELSE round((count(*) FILTER (WHERE event_type = 'CONVERSION') * 120.00 - sum(event_cost)) / sum(event_cost), 4)
       END
FROM dwd.fact_campaign_event
GROUP BY event_date, campaign_code;

ANALYZE;
