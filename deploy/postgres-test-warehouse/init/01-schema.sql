\set ON_ERROR_STOP on

CREATE SCHEMA ods;
CREATE SCHEMA dwd;
CREATE SCHEMA dws;
CREATE SCHEMA ads;

CREATE TABLE ods.customers (
  customer_id integer PRIMARY KEY,
  customer_name text NOT NULL,
  phone text,
  email text,
  region text NOT NULL,
  city text NOT NULL,
  registered_at timestamp NOT NULL,
  source_system text NOT NULL
);

CREATE TABLE ods.products (
  product_id integer PRIMARY KEY,
  product_name text NOT NULL,
  category text NOT NULL,
  supplier_id integer NOT NULL,
  list_price numeric(12,2) NOT NULL,
  created_at timestamp NOT NULL
);

CREATE TABLE ods.orders (
  order_id integer PRIMARY KEY,
  customer_id integer NOT NULL REFERENCES ods.customers(customer_id),
  order_time timestamp NOT NULL,
  status text NOT NULL CHECK (status IN ('PAID','CANCELLED','REFUNDED')),
  order_amount numeric(14,2) NOT NULL DEFAULT 0,
  channel text NOT NULL
);

CREATE TABLE ods.order_items (
  item_id integer PRIMARY KEY,
  order_id integer NOT NULL REFERENCES ods.orders(order_id),
  product_id integer NOT NULL REFERENCES ods.products(product_id),
  quantity integer NOT NULL CHECK (quantity > 0),
  unit_price numeric(12,2) NOT NULL,
  item_amount numeric(14,2) NOT NULL
);

CREATE TABLE ods.payments (
  payment_id integer PRIMARY KEY,
  order_id integer NOT NULL REFERENCES ods.orders(order_id),
  payment_time timestamp NOT NULL,
  payment_method text NOT NULL,
  paid_amount numeric(14,2) NOT NULL CHECK (paid_amount >= 0)
);

CREATE TABLE ods.refunds (
  refund_id integer PRIMARY KEY,
  order_id integer NOT NULL REFERENCES ods.orders(order_id),
  refund_time timestamp NOT NULL,
  refund_reason text NOT NULL,
  refund_amount numeric(14,2) NOT NULL CHECK (refund_amount >= 0)
);

CREATE TABLE ods.inventory_snapshots (
  snapshot_date date NOT NULL,
  product_id integer NOT NULL REFERENCES ods.products(product_id),
  warehouse_code text NOT NULL,
  available_quantity integer NOT NULL,
  PRIMARY KEY (snapshot_date, product_id, warehouse_code)
);

CREATE TABLE ods.campaign_events (
  event_id integer PRIMARY KEY,
  event_time timestamp NOT NULL,
  campaign_code text NOT NULL,
  customer_id integer REFERENCES ods.customers(customer_id),
  event_type text NOT NULL CHECK (event_type IN ('IMPRESSION','CLICK','CONVERSION')),
  event_cost numeric(12,2) NOT NULL DEFAULT 0
);

CREATE TABLE dwd.dim_customer (
  customer_id integer PRIMARY KEY,
  customer_name text NOT NULL,
  region text NOT NULL,
  city text NOT NULL,
  registered_date date NOT NULL
);

CREATE TABLE dwd.dim_product (
  product_id integer PRIMARY KEY,
  product_name text NOT NULL,
  category text NOT NULL,
  supplier_id integer NOT NULL,
  list_price numeric(12,2) NOT NULL
);

CREATE TABLE dwd.fact_order (
  order_id integer PRIMARY KEY,
  customer_id integer NOT NULL REFERENCES dwd.dim_customer(customer_id),
  order_date date NOT NULL,
  status text NOT NULL,
  order_amount numeric(14,2) NOT NULL,
  paid_amount numeric(14,2) NOT NULL,
  refund_amount numeric(14,2) NOT NULL,
  channel text NOT NULL
);

CREATE TABLE dwd.fact_order_item (
  item_id integer PRIMARY KEY,
  order_id integer NOT NULL REFERENCES dwd.fact_order(order_id),
  product_id integer NOT NULL REFERENCES dwd.dim_product(product_id),
  quantity integer NOT NULL,
  unit_price numeric(12,2) NOT NULL,
  item_amount numeric(14,2) NOT NULL
);

CREATE TABLE dwd.fact_payment (
  payment_id integer PRIMARY KEY,
  order_id integer NOT NULL REFERENCES dwd.fact_order(order_id),
  payment_date date NOT NULL,
  payment_method text NOT NULL,
  paid_amount numeric(14,2) NOT NULL
);

CREATE TABLE dwd.fact_campaign_event (
  event_id integer PRIMARY KEY,
  event_date date NOT NULL,
  campaign_code text NOT NULL,
  customer_id integer REFERENCES dwd.dim_customer(customer_id),
  event_type text NOT NULL,
  event_cost numeric(12,2) NOT NULL
);

CREATE TABLE dws.user_day (
  stat_date date NOT NULL,
  customer_id integer NOT NULL REFERENCES dwd.dim_customer(customer_id),
  paid_order_count integer NOT NULL,
  paid_amount numeric(14,2) NOT NULL,
  refund_amount numeric(14,2) NOT NULL,
  PRIMARY KEY (stat_date, customer_id)
);

CREATE TABLE dws.product_day (
  stat_date date NOT NULL,
  product_id integer NOT NULL REFERENCES dwd.dim_product(product_id),
  paid_quantity integer NOT NULL,
  paid_amount numeric(14,2) NOT NULL,
  PRIMARY KEY (stat_date, product_id)
);

CREATE TABLE dws.region_day (
  stat_date date NOT NULL,
  region text NOT NULL,
  paid_user_count integer NOT NULL,
  paid_order_count integer NOT NULL,
  paid_amount numeric(14,2) NOT NULL,
  PRIMARY KEY (stat_date, region)
);

CREATE TABLE ads.daily_trade (
  stat_date date PRIMARY KEY,
  gmv numeric(14,2) NOT NULL,
  paid_order_count integer NOT NULL,
  paid_user_count integer NOT NULL,
  refund_amount numeric(14,2) NOT NULL,
  net_sales numeric(14,2) NOT NULL
);

CREATE TABLE ads.user_retention (
  stat_month date NOT NULL,
  cohort_month date NOT NULL,
  active_users integer NOT NULL,
  retained_users integer NOT NULL,
  retention_rate numeric(8,4) NOT NULL,
  PRIMARY KEY (stat_month, cohort_month)
);

CREATE TABLE ads.campaign_roi (
  stat_date date NOT NULL,
  campaign_code text NOT NULL,
  impressions integer NOT NULL,
  clicks integer NOT NULL,
  conversions integer NOT NULL,
  spend numeric(14,2) NOT NULL,
  attributed_revenue numeric(14,2) NOT NULL,
  roi numeric(12,4),
  PRIMARY KEY (stat_date, campaign_code)
);

COMMENT ON SCHEMA ods IS '数仓分层=ODS;用途=原始业务数据';
COMMENT ON SCHEMA dwd IS '数仓分层=DWD;用途=清洗后的维度与事实明细';
COMMENT ON SCHEMA dws IS '数仓分层=DWS;用途=按主题汇总';
COMMENT ON SCHEMA ads IS '数仓分层=ADS;用途=应用指标与经营分析';

COMMENT ON TABLE ods.customers IS '业务域=电商;分层=ODS;粒度=每位客户一行;敏感字段=姓名、手机号、邮箱';
COMMENT ON TABLE ods.products IS '业务域=供应链;分层=ODS;粒度=每个商品一行';
COMMENT ON TABLE ods.orders IS '业务域=电商;分层=ODS;粒度=每笔订单一行';
COMMENT ON TABLE ods.order_items IS '业务域=电商;分层=ODS;粒度=每个订单商品明细一行';
COMMENT ON TABLE ods.payments IS '业务域=电商;分层=ODS;粒度=每次支付一行;一个订单可以多次支付';
COMMENT ON TABLE ods.refunds IS '业务域=电商;分层=ODS;粒度=每次退款一行';
COMMENT ON TABLE ods.inventory_snapshots IS '业务域=供应链;分层=ODS;粒度=日期、商品、仓库';
COMMENT ON TABLE ods.campaign_events IS '业务域=营销;分层=ODS;粒度=每次营销事件一行';
COMMENT ON TABLE dwd.dim_customer IS '业务域=电商;分层=DWD;粒度=每位客户一行;不含电话和邮箱';
COMMENT ON TABLE dwd.dim_product IS '业务域=供应链;分层=DWD;粒度=每个商品一行';
COMMENT ON TABLE dwd.fact_order IS '业务域=电商;分层=DWD;粒度=每笔订单一行;支付与退款已汇总';
COMMENT ON TABLE dwd.fact_order_item IS '业务域=电商;分层=DWD;粒度=每个订单商品明细一行';
COMMENT ON TABLE dwd.fact_payment IS '业务域=电商;分层=DWD;粒度=每次支付一行';
COMMENT ON TABLE dwd.fact_campaign_event IS '业务域=营销;分层=DWD;粒度=每次营销事件一行';
COMMENT ON TABLE dws.user_day IS '业务域=电商;分层=DWS;粒度=日期、客户';
COMMENT ON TABLE dws.product_day IS '业务域=供应链;分层=DWS;粒度=日期、商品';
COMMENT ON TABLE dws.region_day IS '业务域=电商;分层=DWS;粒度=日期、区域';
COMMENT ON TABLE ads.daily_trade IS '业务域=电商;分层=ADS;粒度=自然日;指标=GMV、订单量、活跃购买用户、退款、净销售额';
COMMENT ON TABLE ads.user_retention IS '业务域=电商;分层=ADS;粒度=统计月、注册月;指标=月留存率';
COMMENT ON TABLE ads.campaign_roi IS '业务域=营销;分层=ADS;粒度=日期、活动;指标=曝光、点击、转化、花费、归因收入、ROI';

COMMENT ON COLUMN ods.customers.customer_id IS '客户主键;业务别名=用户ID';
COMMENT ON COLUMN ods.customers.customer_name IS '客户姓名;敏感级别=高;展示规则=脱敏';
COMMENT ON COLUMN ods.customers.phone IS '手机号;敏感级别=高;展示规则=仅保留前三后四';
COMMENT ON COLUMN ods.customers.email IS '邮箱;敏感级别=高;展示规则=隐藏用户名中段';
COMMENT ON COLUMN ods.orders.order_time IS '下单时间;默认订单时间口径';
COMMENT ON COLUMN ods.orders.status IS '订单状态;PAID=已支付,CANCELLED=已取消,REFUNDED=已退款';
COMMENT ON COLUMN ods.orders.order_amount IS '订单商品明细金额合计;不等同于实付金额';
COMMENT ON COLUMN ods.payments.payment_time IS '支付发生时间;支付时间口径';
COMMENT ON COLUMN ods.payments.paid_amount IS '本次支付金额;订单级分析需先按订单汇总避免一对多重复';
COMMENT ON COLUMN ods.refunds.refund_amount IS '本次退款金额';
COMMENT ON COLUMN ads.daily_trade.gmv IS 'GMV=状态为PAID或REFUNDED订单的实付金额合计';
COMMENT ON COLUMN ads.daily_trade.net_sales IS '净销售额=GMV-退款金额';
COMMENT ON COLUMN ads.user_retention.retention_rate IS '月留存率=留存用户数/同期活跃用户数';
COMMENT ON COLUMN ads.campaign_roi.roi IS 'ROI=(归因收入-营销花费)/营销花费;花费为0时为空';
