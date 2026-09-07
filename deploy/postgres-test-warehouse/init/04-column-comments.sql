\set ON_ERROR_STOP on

DO $$
DECLARE
  field record;
  meaning text;
BEGIN
  FOR field IN
    SELECT c.table_schema, c.table_name, c.column_name, c.ordinal_position
    FROM information_schema.columns c
    WHERE c.table_schema IN ('ods','dwd','dws','ads')
      AND col_description(
        format('%I.%I', c.table_schema, c.table_name)::regclass::oid,
        c.ordinal_position
      ) IS NULL
    ORDER BY c.table_schema, c.table_name, c.ordinal_position
  LOOP
    meaning := CASE field.column_name
      WHEN 'customer_id' THEN '客户唯一标识;业务别名=用户ID'
      WHEN 'customer_name' THEN '客户展示名称;敏感级别=高;展示时脱敏'
      WHEN 'phone' THEN '客户手机号;敏感级别=高;仅保留前三后四'
      WHEN 'email' THEN '客户邮箱;敏感级别=高;隐藏用户名中段'
      WHEN 'region' THEN '客户或交易所属大区'
      WHEN 'city' THEN '客户所属城市'
      WHEN 'registered_at' THEN '客户注册时间'
      WHEN 'registered_date' THEN '客户注册自然日'
      WHEN 'source_system' THEN '原始记录来源系统'
      WHEN 'product_id' THEN '商品唯一标识'
      WHEN 'product_name' THEN '商品名称'
      WHEN 'category' THEN '商品业务品类'
      WHEN 'supplier_id' THEN '供应商唯一标识'
      WHEN 'list_price' THEN '商品目录标价;币种=CNY'
      WHEN 'created_at' THEN '商品创建时间'
      WHEN 'order_id' THEN '订单唯一标识'
      WHEN 'order_time' THEN '下单时间;默认订单时间口径'
      WHEN 'order_date' THEN '下单自然日;由order_time转换'
      WHEN 'status' THEN '订单状态;PAID=已支付,CANCELLED=已取消,REFUNDED=已退款'
      WHEN 'order_amount' THEN '订单商品明细金额合计;币种=CNY'
      WHEN 'channel' THEN '订单来源渠道'
      WHEN 'item_id' THEN '订单商品明细唯一标识'
      WHEN 'quantity' THEN '本明细购买数量'
      WHEN 'unit_price' THEN '本明细成交单价;币种=CNY'
      WHEN 'item_amount' THEN '明细金额=数量乘成交单价;币种=CNY'
      WHEN 'payment_id' THEN '支付流水唯一标识'
      WHEN 'payment_time' THEN '支付发生时间;支付时间口径'
      WHEN 'payment_date' THEN '支付自然日;由payment_time转换'
      WHEN 'payment_method' THEN '支付方式'
      WHEN 'paid_amount' THEN '实付金额;订单级使用时先按订单汇总;币种=CNY'
      WHEN 'refund_id' THEN '退款流水唯一标识'
      WHEN 'refund_time' THEN '退款发生时间'
      WHEN 'refund_reason' THEN '退款原因分类'
      WHEN 'refund_amount' THEN '退款金额;币种=CNY'
      WHEN 'snapshot_date' THEN '库存快照自然日'
      WHEN 'warehouse_code' THEN '履约仓库编码'
      WHEN 'available_quantity' THEN '快照时点可售库存数量'
      WHEN 'event_id' THEN '营销事件唯一标识'
      WHEN 'event_time' THEN '营销事件发生时间'
      WHEN 'event_date' THEN '营销事件自然日;由event_time转换'
      WHEN 'campaign_code' THEN '营销活动编码'
      WHEN 'event_type' THEN '事件类型;IMPRESSION=曝光,CLICK=点击,CONVERSION=转化'
      WHEN 'event_cost' THEN '单次营销事件成本;币种=CNY'
      WHEN 'stat_date' THEN '指标统计自然日'
      WHEN 'paid_order_count' THEN '已支付及发生退款的有效订单数'
      WHEN 'paid_quantity' THEN '有效订单中的商品销售数量'
      WHEN 'paid_user_count' THEN '有效订单去重购买客户数'
      WHEN 'gmv' THEN 'GMV=状态为PAID或REFUNDED订单的实付金额合计;币种=CNY'
      WHEN 'net_sales' THEN '净销售额=GMV-退款金额;币种=CNY'
      WHEN 'stat_month' THEN '留存指标统计月;取月初日期'
      WHEN 'cohort_month' THEN '客户注册分群月份;取月初日期'
      WHEN 'active_users' THEN '统计月活跃客户数'
      WHEN 'retained_users' THEN '注册分群在统计月仍活跃的客户数'
      WHEN 'retention_rate' THEN '月留存率=留存客户数/同期活跃客户数'
      WHEN 'impressions' THEN '营销活动曝光次数'
      WHEN 'clicks' THEN '营销活动点击次数'
      WHEN 'conversions' THEN '营销活动转化次数'
      WHEN 'spend' THEN '营销活动花费;币种=CNY'
      WHEN 'attributed_revenue' THEN '归因到营销活动的收入;币种=CNY'
      WHEN 'roi' THEN 'ROI=(归因收入-营销花费)/营销花费;花费为0时为空'
      ELSE '合成测试数仓业务字段'
    END;
    EXECUTE format(
      'COMMENT ON COLUMN %I.%I.%I IS %L',
      field.table_schema,
      field.table_name,
      field.column_name,
      meaning
    );
  END LOOP;
END $$;
