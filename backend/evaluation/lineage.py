"""Synthetic-only evidence fixture, shared by HTTP and browser acceptance."""
import sqlite3

GOLDEN_SQL = """INSERT INTO sales_by_region(stat_date,region,paid_amount)
 SELECT DATE(o.order_time),c.region,SUM(o.order_amount)
 FROM fact_orders o JOIN customers c ON o.customer_id=c.customer_id
 WHERE o.status='PAID' GROUP BY DATE(o.order_time),c.region"""
DOCUMENT = '订单 fact_orders.customer_id 与客户 customers.customer_id 关联。仅 PAID 订单的 order_amount 聚合为 sales_by_region.paid_amount。'


def prepare_lineage_fixture(factory,warehouse):
    physical=sqlite3.connect(warehouse)
    physical.executescript('''CREATE TABLE fact_orders(order_id INTEGER,customer_id INTEGER,order_time TEXT,order_amount NUMERIC,status TEXT);
        CREATE TABLE customers(customer_id INTEGER,region TEXT);
        CREATE TABLE sales_by_region(stat_date TEXT,region TEXT,paid_amount NUMERIC);
        INSERT INTO customers VALUES (1,'East'),(2,'West');
        INSERT INTO fact_orders VALUES (1,1,'2026-08-01',100,'PAID'),(2,2,'2026-08-01',50,'CANCELLED');''')
    physical.commit();physical.close()
    con=factory()
    try:
        fields={'fact_orders':[('order_id','INTEGER'),('customer_id','INTEGER'),('order_time','TEXT'),('order_amount','NUMERIC'),('status','TEXT')],
            'customers':[('customer_id','INTEGER'),('region','TEXT')],'sales_by_region':[('stat_date','TEXT'),('region','TEXT'),('paid_amount','NUMERIC')]}
        for name,cols in fields.items():
            con.execute('''INSERT INTO catalog_assets(id,source_id,namespace,physical_name,display_name,domain_id,publish_status)
                VALUES (?,'local-ecommerce-demo','',?,?,'domain-ecommerce','published')''',('test-'+name,name,name))
            for index,(column,kind) in enumerate(cols):
                con.execute('''INSERT INTO catalog_columns(id,asset_id,physical_name,display_name,data_type,ordinal)
                    VALUES (?,?,?,?,?,?)''',('test-'+name+':'+column,'test-'+name,column,column,kind,index))
        con.commit()
    finally:con.close()
