"""Edge cases deliberately absent from happy-path demo data; never seed the live DB."""
import sqlite3


def create_warehouse(path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE users(user_id INTEGER PRIMARY KEY,name TEXT,region TEXT,city TEXT);
    CREATE TABLE products(product_id INTEGER PRIMARY KEY,name TEXT,category TEXT,price REAL);
    CREATE TABLE orders(order_id INTEGER PRIMARY KEY,user_id INTEGER,product_id INTEGER,amount REAL,status TEXT,order_date TEXT,quantity INTEGER);
    CREATE TABLE dwd_payment_detail(payment_id INTEGER PRIMARY KEY,order_id INTEGER,paid_amount REAL,payment_status TEXT);
    CREATE TABLE ads_campaign_roi(roi_key TEXT,stat_date TEXT,campaign_id TEXT,channel TEXT,cost REAL,attributed_gmv REAL,roi REAL,conversion_rate REAL);
    INSERT INTO users VALUES (1,'示例甲','华东','上海'),(2,'示例乙','华南','深圳'),(3,'示例丙',NULL,NULL),(4,'示例丁','华北','北京');
    INSERT INTO products VALUES (1,'示例商品一','数码',100),(2,'示例商品二','食品',50);
    INSERT INTO orders VALUES
      (1,1,1,100,'PAID','2026-08-01',1),
      (2,1,2,50,'PAID','2026-08-31',1),
      (3,2,1,200,'REFUNDED','2026-08-15',2),
      (4,2,2,75,'CANCELLED','2026-08-16',1),
      (5,2,1,300,'PAID','2026-09-01',3),
      (6,3,2,40,'PAID','2026-07-31',1),
      (7,3,2,NULL,'PAID','2026-08-20',1),
      (8,2,2,0,'PAID','2026-08-25',1);
    INSERT INTO dwd_payment_detail VALUES (1,1,60,'SUCCESS'),(2,1,40,'SUCCESS'),(3,2,50,'SUCCESS');
    INSERT INTO ads_campaign_roi VALUES ('roi1','2026-08-01','c1','search',100,200,2,0.1);
    """)
    con.commit()
    con.close()
