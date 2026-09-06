"""Rebuild the explicitly-local fake warehouse and its catalog metadata.

This script targets only ``.data/demo.db`` and the checked-in demo metadata.
It is intentionally not part of normal application startup: real deployments
must obtain technical metadata from an approved source connector instead.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO_DB = ROOT / ".data" / "demo.db"
CATALOG_PATH = ROOT / "metadata" / "catalog.json"
RELATIONS_PATH = ROOT / "metadata" / "relations.json"


def column(name: str, typ: str, description: str, role: str = "attribute", aliases: list[str] | None = None) -> dict:
    return {"name": name, "type": typ, "description": description, "semantic_role": role, "aliases": aliases or []}


def table(name: str, display_name: str, domain_id: str, layer: str, stage: str, description: str, grain: str, primary_key: str, columns: list[dict], aliases: list[str] | None = None, sensitivity: str = "internal") -> dict:
    return {"name": name, "display_name": display_name, "domain_id": domain_id, "warehouse_layer": layer, "normalized_stage": stage, "description": description, "data_grain": grain, "primary_key": primary_key, "columns": columns, "aliases": aliases or [], "sensitivity": sensitivity}


CATALOG = {
    "version": "2.0",
    "source": {"id": "local-ecommerce-demo", "display_name": "本地全域经营演示数仓", "type": "sqlite", "synced_at": "2026-08-26"},
    "domains": [
        {"id": "domain-ecommerce", "name": "ecommerce", "display_name": "电商经营域", "description": "用户、商品、订单、支付与经营看板资产。"},
        {"id": "domain-marketing", "name": "marketing", "display_name": "营销增长域", "description": "投放事件、归因、转化与 ROI 资产。"},
    ],
    "tables": [
        table("ods_user_snapshot", "用户快照原始表", "domain-ecommerce", "ODS", "raw", "每日从用户系统采集的原始快照，保留源系统字段。", "一行代表一个用户在一个快照日期的记录。", "snapshot_id", [column("snapshot_id","INTEGER","原始快照唯一标识。","primary_key"),column("snapshot_date","TEXT","采集快照日期。","event_time"),column("user_id","INTEGER","源用户标识。","foreign_key"),column("user_name","TEXT","源系统用户姓名。","attribute"),column("region","TEXT","源系统大区。","dimension"),column("city","TEXT","源系统城市。","dimension")], ["原始用户", "用户 ODS"]),
        table("ods_product_snapshot", "商品快照原始表", "domain-ecommerce", "ODS", "raw", "每日从商品系统采集的原始商品快照。", "一行代表一个商品在一个快照日期的记录。", "snapshot_id", [column("snapshot_id","INTEGER","原始快照唯一标识。","primary_key"),column("snapshot_date","TEXT","采集快照日期。","event_time"),column("product_id","INTEGER","源商品标识。","foreign_key"),column("product_name","TEXT","源商品名称。"),column("category","TEXT","源商品品类。","dimension"),column("list_price","REAL","源系统目录标价。","measure")], ["原始商品", "商品 ODS"]),
        table("ods_order_events", "订单事件原始表", "domain-ecommerce", "ODS", "raw", "订单系统产生的下单、支付、取消等事件流。", "一行代表一笔订单的一次状态事件。", "event_id", [column("event_id","INTEGER","订单事件唯一标识。","primary_key"),column("event_time","TEXT","事件发生时间。","event_time"),column("order_id","INTEGER","源订单标识。","foreign_key"),column("user_id","INTEGER","下单用户标识。","foreign_key"),column("product_id","INTEGER","商品标识。","foreign_key"),column("event_type","TEXT","事件类型，如 created、paid。","status"),column("event_amount","REAL","事件对应金额。","measure")], ["订单 ODS", "交易事件"]),
        table("users", "用户维表", "domain-ecommerce", "DIM", "refined", "已清洗的平台注册用户维度，用于按区域、城市分析业务。", "一行代表一个用户。", "user_id", [column("user_id","INTEGER","用户唯一标识。","primary_key",["用户ID","客户ID"]),column("name","TEXT","用户姓名，样例展示会脱敏。","attribute",["用户名","客户姓名"]),column("region","TEXT","用户所属销售大区。","dimension",["大区","销售区域"]),column("city","TEXT","用户所在城市。","dimension",["城市","所在地"]),column("register_date","TEXT","用户注册日期。","event_time")], ["用户表", "会员表", "客户信息表"]),
        table("products", "商品维表", "domain-ecommerce", "DIM", "refined", "已清洗的商品维度，用于按商品、品类分析交易。", "一行代表一个商品。", "product_id", [column("product_id","INTEGER","商品唯一标识。","primary_key",["商品ID","SKU ID"]),column("name","TEXT","商品名称。","attribute",["商品名","SKU名称"]),column("category","TEXT","商品所属品类。","dimension",["品类","商品分类"]),column("unit_price","REAL","商品目录标价，不等同于订单实付金额。","measure"),column("brand","TEXT","商品品牌。","dimension")], ["商品表", "商品信息表", "SKU表"]),
        table("dim_calendar", "日期维表", "domain-ecommerce", "DIM", "refined", "标准日期维度，为日、周、月经营分析提供统一时间口径。", "一行代表一个自然日期。", "date_key", [column("date_key","TEXT","日期键，YYYY-MM-DD。","primary_key"),column("month_key","TEXT","自然月键，YYYY-MM。","dimension"),column("week_of_year","INTEGER","一年中的第几周。","dimension"),column("is_weekend","INTEGER","是否周末，1 表示是。","dimension")], ["日历维", "日期表"]),
        table("orders", "订单明细表", "domain-ecommerce", "DWD", "refined", "已清洗的订单明细，用于订单量、实付金额、销量及用户购买行为分析。", "一行代表一笔订单记录。", "order_id", [column("order_id","INTEGER","订单唯一标识。","primary_key",["订单ID","订单号"]),column("order_date","TEXT","用户下单日期。","event_time",["下单日期","订单日期"]),column("user_id","INTEGER","下单用户标识。","foreign_key",["用户ID"]),column("product_id","INTEGER","购买商品标识。","foreign_key",["商品ID"]),column("quantity","INTEGER","本订单购买的商品数量。","measure",["购买数量","销量"]),column("amount","REAL","订单实付金额。","measure",["实付金额","支付金额"]),column("status","TEXT","订单状态：PAID、CANCELLED、REFUNDED。","status")], ["订单表", "交易表", "销售订单"]),
        table("dwd_payment_detail", "支付明细表", "domain-ecommerce", "DWD", "refined", "支付渠道返回的清洗支付明细，用于支付成功率和渠道分析。", "一行代表一次支付尝试。", "payment_id", [column("payment_id","INTEGER","支付明细唯一标识。","primary_key"),column("order_id","INTEGER","关联订单标识。","foreign_key"),column("pay_date","TEXT","支付日期。","event_time"),column("payment_channel","TEXT","支付渠道，如支付宝、微信。","dimension"),column("paid_amount","REAL","实际支付金额。","measure"),column("payment_status","TEXT","支付状态。","status")], ["支付表", "收款明细"]),
        table("dws_daily_sales", "日销售汇总表", "domain-ecommerce", "DWS", "aggregate", "按日期、区域和品类汇总的可信经营指标。", "一行代表一个日期、区域、品类组合。", "sales_key", [column("sales_key","TEXT","汇总记录唯一键。","primary_key"),column("sales_date","TEXT","统计日期。","event_time"),column("region","TEXT","销售大区。","dimension"),column("category","TEXT","商品品类。","dimension"),column("paid_order_count","INTEGER","已支付订单数。","measure"),column("gmv","REAL","已支付订单实付金额之和。","measure"),column("units_sold","INTEGER","已支付订单商品件数之和。","measure")], ["日销汇总", "销售宽表"]),
        table("dws_user_purchase_30d", "用户近30日购买汇总表", "domain-ecommerce", "DWS", "aggregate", "按用户滚动聚合的近 30 日购买行为，用于复购和用户价值分析。", "一行代表一个用户在统计日期的滚动窗口汇总。", "user_purchase_key", [column("user_purchase_key","TEXT","用户窗口汇总唯一键。","primary_key"),column("stat_date","TEXT","统计截止日期。","event_time"),column("user_id","INTEGER","用户标识。","foreign_key"),column("paid_order_count_30d","INTEGER","近30日已支付订单数。","measure"),column("gmv_30d","REAL","近30日 GMV。","measure"),column("last_paid_date","TEXT","最近一次支付日期。","event_time")], ["用户购买汇总", "用户价值宽表"]),
        table("ads_sales_dashboard", "销售经营看板表", "domain-ecommerce", "ADS", "application", "服务销售经营看板的日级应用数据集，默认推荐给分析问答。", "一行代表一个统计日期和区域。", "dashboard_key", [column("dashboard_key","TEXT","看板记录唯一键。","primary_key"),column("stat_date","TEXT","统计日期。","event_time"),column("region","TEXT","销售大区。","dimension"),column("gmv","REAL","已支付 GMV。","measure"),column("paid_order_count","INTEGER","已支付订单量。","measure"),column("aov","REAL","客单价，GMV/订单量。","measure"),column("active_buyer_count","INTEGER","当日支付用户数。","measure")], ["销售看板", "经营大盘"]),
        table("ods_campaign_events", "营销活动事件原始表", "domain-marketing", "ODS", "raw", "广告平台采集的曝光、点击、转化原始事件。", "一行代表一次营销触点事件。", "event_id", [column("event_id","INTEGER","活动事件唯一标识。","primary_key"),column("event_date","TEXT","事件日期。","event_time"),column("campaign_id","TEXT","活动标识。","dimension"),column("channel","TEXT","投放渠道。","dimension"),column("event_type","TEXT","曝光、点击或转化事件。","status"),column("cost","REAL","该触点归属成本。","measure"),column("user_id","INTEGER","可归因用户标识。","foreign_key")], ["营销 ODS", "投放事件"]),
        table("dwd_campaign_attribution", "营销归因明细表", "domain-marketing", "DWD", "refined", "将广告触点与订单转化归因后的明细。", "一行代表一笔归因转化。", "attribution_id", [column("attribution_id","INTEGER","归因记录唯一标识。","primary_key"),column("event_date","TEXT","归因日期。","event_time"),column("campaign_id","TEXT","活动标识。","dimension"),column("user_id","INTEGER","转化用户标识。","foreign_key"),column("order_id","INTEGER","归因订单标识。","foreign_key"),column("attributed_gmv","REAL","归因后的成交金额。","measure"),column("attribution_model","TEXT","归因模型，如 last_click。","dimension")], ["活动归因", "投放转化"]),
        table("dws_campaign_daily", "活动日汇总表", "domain-marketing", "DWS", "aggregate", "按日期、活动、渠道汇总投放成本和转化结果。", "一行代表一个日期、活动、渠道组合。", "campaign_daily_key", [column("campaign_daily_key","TEXT","活动日汇总唯一键。","primary_key"),column("stat_date","TEXT","统计日期。","event_time"),column("campaign_id","TEXT","活动标识。","dimension"),column("channel","TEXT","投放渠道。","dimension"),column("impressions","INTEGER","曝光次数。","measure"),column("clicks","INTEGER","点击次数。","measure"),column("conversions","INTEGER","转化订单数。","measure"),column("cost","REAL","投放成本。","measure"),column("attributed_gmv","REAL","归因 GMV。","measure")], ["活动效果汇总", "投放日报"]),
        table("ads_campaign_roi", "营销 ROI 看板表", "domain-marketing", "ADS", "application", "服务营销投放效率看板的应用数据集。", "一行代表一个统计日期、活动、渠道组合。", "roi_key", [column("roi_key","TEXT","ROI 看板记录唯一键。","primary_key"),column("stat_date","TEXT","统计日期。","event_time"),column("campaign_id","TEXT","活动标识。","dimension"),column("channel","TEXT","投放渠道。","dimension"),column("cost","REAL","投放成本。","measure"),column("attributed_gmv","REAL","归因 GMV。","measure"),column("roi","REAL","ROI，归因 GMV/成本。","measure"),column("conversion_rate","REAL","转化率，转化数/点击数。","measure")], ["ROI 看板", "营销大盘"]),
    ],
}

RELATIONS = [
    ("orders_user", "orders", "user_id", "users", "user_id", "订单关联用户，获取区域和城市属性。"),
    ("orders_product", "orders", "product_id", "products", "product_id", "订单关联商品，获取名称、品类和标价。"),
    ("payment_order", "dwd_payment_detail", "order_id", "orders", "order_id", "支付明细关联订单，核对支付与交易状态。"),
    ("purchase_user", "dws_user_purchase_30d", "user_id", "users", "user_id", "用户近30日汇总关联用户维度。"),
    ("attribution_user", "dwd_campaign_attribution", "user_id", "users", "user_id", "归因明细关联转化用户。"),
    ("attribution_order", "dwd_campaign_attribution", "order_id", "orders", "order_id", "归因明细关联转化订单。"),
]


def reset_demo_database() -> None:
    """Recreate only the local fake warehouse, including derived aggregate tables."""
    DEMO_DB.parent.mkdir(exist_ok=True)
    if DEMO_DB.exists(): DEMO_DB.unlink()
    con = sqlite3.connect(DEMO_DB)
    con.executescript("""
    CREATE TABLE ods_user_snapshot(snapshot_id INTEGER PRIMARY KEY,snapshot_date TEXT,user_id INTEGER,user_name TEXT,region TEXT,city TEXT);
    CREATE TABLE ods_product_snapshot(snapshot_id INTEGER PRIMARY KEY,snapshot_date TEXT,product_id INTEGER,product_name TEXT,category TEXT,list_price REAL);
    CREATE TABLE ods_order_events(event_id INTEGER PRIMARY KEY,event_time TEXT,order_id INTEGER,user_id INTEGER,product_id INTEGER,event_type TEXT,event_amount REAL);
    CREATE TABLE users(user_id INTEGER PRIMARY KEY,name TEXT,region TEXT,city TEXT,register_date TEXT);
    CREATE TABLE products(product_id INTEGER PRIMARY KEY,name TEXT,category TEXT,unit_price REAL,brand TEXT);
    CREATE TABLE dim_calendar(date_key TEXT PRIMARY KEY,month_key TEXT,week_of_year INTEGER,is_weekend INTEGER);
    CREATE TABLE orders(order_id INTEGER PRIMARY KEY,order_date TEXT,user_id INTEGER,product_id INTEGER,quantity INTEGER,amount REAL,status TEXT);
    CREATE TABLE dwd_payment_detail(payment_id INTEGER PRIMARY KEY,order_id INTEGER,pay_date TEXT,payment_channel TEXT,paid_amount REAL,payment_status TEXT);
    CREATE TABLE dws_daily_sales(sales_key TEXT PRIMARY KEY,sales_date TEXT,region TEXT,category TEXT,paid_order_count INTEGER,gmv REAL,units_sold INTEGER);
    CREATE TABLE dws_user_purchase_30d(user_purchase_key TEXT PRIMARY KEY,stat_date TEXT,user_id INTEGER,paid_order_count_30d INTEGER,gmv_30d REAL,last_paid_date TEXT);
    CREATE TABLE ads_sales_dashboard(dashboard_key TEXT PRIMARY KEY,stat_date TEXT,region TEXT,gmv REAL,paid_order_count INTEGER,aov REAL,active_buyer_count INTEGER);
    CREATE TABLE ods_campaign_events(event_id INTEGER PRIMARY KEY,event_date TEXT,campaign_id TEXT,channel TEXT,event_type TEXT,cost REAL,user_id INTEGER);
    CREATE TABLE dwd_campaign_attribution(attribution_id INTEGER PRIMARY KEY,event_date TEXT,campaign_id TEXT,user_id INTEGER,order_id INTEGER,attributed_gmv REAL,attribution_model TEXT);
    CREATE TABLE dws_campaign_daily(campaign_daily_key TEXT PRIMARY KEY,stat_date TEXT,campaign_id TEXT,channel TEXT,impressions INTEGER,clicks INTEGER,conversions INTEGER,cost REAL,attributed_gmv REAL);
    CREATE TABLE ads_campaign_roi(roi_key TEXT PRIMARY KEY,stat_date TEXT,campaign_id TEXT,channel TEXT,cost REAL,attributed_gmv REAL,roi REAL,conversion_rate REAL);
    """)
    users = [(1,"王芳","华东","上海","2025-10-02"),(2,"李明","华东","杭州","2025-11-11"),(3,"张伟","华南","深圳","2026-01-05"),(4,"陈晨","华北","北京","2026-02-17"),(5,"刘洋","华南","广州","2026-03-03"),(6,"赵敏","西南","成都","2026-04-15"),(7,"孙磊","华东","南京","2026-05-09"),(8,"周婷","华北","天津","2026-06-22")]
    products = [(101,"轻羽跑鞋","运动鞋",499,"Aster"),(102,"通勤双肩包","箱包",269,"North"),(103,"无线降噪耳机","数码",799,"SoundX"),(104,"棉质短袖","服饰",129,"Mori"),(105,"不粘煎锅","家居",189,"Homey"),(106,"智能水杯","家居",159,"Homey")]
    orders = [(1001,"2026-08-10",1,101,1,469,"PAID"),(1002,"2026-08-10",2,102,1,249,"PAID"),(1003,"2026-08-11",3,103,1,759,"PAID"),(1004,"2026-08-11",4,104,2,238,"PAID"),(1005,"2026-08-12",5,105,1,179,"CANCELLED"),(1006,"2026-08-12",6,101,1,499,"PAID"),(1007,"2026-08-13",7,106,2,298,"PAID"),(1008,"2026-08-13",1,103,1,799,"REFUNDED"),(1009,"2026-08-14",8,104,3,357,"PAID"),(1010,"2026-08-15",2,105,2,358,"PAID"),(1011,"2026-08-16",3,102,1,269,"PAID"),(1012,"2026-08-17",4,101,1,479,"PAID")]
    con.executemany("INSERT INTO users VALUES (?,?,?,?,?)",users); con.executemany("INSERT INTO products VALUES (?,?,?,?,?)",products); con.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)",orders)
    con.executemany("INSERT INTO ods_user_snapshot VALUES (?,?,?,?,?,?)", [(index,"2026-08-17",*item[:4]) for index,item in enumerate(users,1)])
    con.executemany("INSERT INTO ods_product_snapshot VALUES (?,?,?,?,?,?)", [(index,"2026-08-17",item[0],item[1],item[2],item[3]) for index,item in enumerate(products,1)])
    events=[]; event_id=1
    for order in orders:
        events.append((event_id,order[1]+"T09:00:00",order[0],order[2],order[3],"created",order[5])); event_id+=1
        if order[6] in {"PAID","REFUNDED"}: events.append((event_id,order[1]+"T09:03:00",order[0],order[2],order[3],"paid",order[5])); event_id+=1
    con.executemany("INSERT INTO ods_order_events VALUES (?,?,?,?,?,?,?)",events)
    calendar=[(f"2026-08-{day:02d}","2026-08",33,(day%7 in {1,2})) for day in range(8,19)]; con.executemany("INSERT INTO dim_calendar VALUES (?,?,?,?)",calendar)
    payments=[(index,order[0],order[1],"支付宝" if index%2 else "微信支付",order[5],"SUCCESS" if order[6] in {"PAID","REFUNDED"} else "FAILED") for index,order in enumerate(orders,1)]; con.executemany("INSERT INTO dwd_payment_detail VALUES (?,?,?,?,?,?)",payments)
    con.execute("""INSERT INTO dws_daily_sales SELECT o.order_date||'|'||u.region||'|'||p.category,o.order_date,u.region,p.category,COUNT(*),ROUND(SUM(o.amount),2),SUM(o.quantity) FROM orders o JOIN users u ON o.user_id=u.user_id JOIN products p ON o.product_id=p.product_id WHERE o.status='PAID' GROUP BY 2,3,4""")
    con.execute("""INSERT INTO dws_user_purchase_30d SELECT '2026-08-17|'||user_id,'2026-08-17',user_id,COUNT(*),ROUND(SUM(amount),2),MAX(order_date) FROM orders WHERE status='PAID' GROUP BY user_id""")
    con.execute("""INSERT INTO ads_sales_dashboard SELECT o.order_date||'|'||u.region,o.order_date,u.region,ROUND(SUM(o.amount),2),COUNT(*),ROUND(SUM(o.amount)*1.0/COUNT(*),2),COUNT(DISTINCT o.user_id) FROM orders o JOIN users u ON o.user_id=u.user_id WHERE o.status='PAID' GROUP BY 2,3""")
    campaign_events=[(1,"2026-08-10","CMP-SUMMER","抖音","impression",20,1),(2,"2026-08-10","CMP-SUMMER","抖音","click",10,1),(3,"2026-08-10","CMP-SUMMER","抖音","conversion",0,1),(4,"2026-08-11","CMP-NEW","小红书","impression",24,3),(5,"2026-08-11","CMP-NEW","小红书","click",12,3),(6,"2026-08-11","CMP-NEW","小红书","conversion",0,3),(7,"2026-08-13","CMP-SUMMER","微信","impression",18,7),(8,"2026-08-13","CMP-SUMMER","微信","click",9,7),(9,"2026-08-13","CMP-SUMMER","微信","conversion",0,7)]
    attribution=[(1,"2026-08-10","CMP-SUMMER",1,1001,469,"last_click"),(2,"2026-08-11","CMP-NEW",3,1003,759,"last_click"),(3,"2026-08-13","CMP-SUMMER",7,1007,298,"last_click")]
    con.executemany("INSERT INTO ods_campaign_events VALUES (?,?,?,?,?,?,?)",campaign_events); con.executemany("INSERT INTO dwd_campaign_attribution VALUES (?,?,?,?,?,?,?)",attribution)
    con.execute("""INSERT INTO dws_campaign_daily SELECT e.event_date||'|'||e.campaign_id||'|'||e.channel,e.event_date,e.campaign_id,e.channel,SUM(e.event_type='impression'),SUM(e.event_type='click'),SUM(e.event_type='conversion'),ROUND(SUM(e.cost),2),COALESCE((SELECT SUM(a.attributed_gmv) FROM dwd_campaign_attribution a WHERE a.event_date=e.event_date AND a.campaign_id=e.campaign_id),0) FROM ods_campaign_events e GROUP BY 2,3,4""")
    con.execute("""INSERT INTO ads_campaign_roi SELECT campaign_daily_key,stat_date,campaign_id,channel,cost,attributed_gmv,ROUND(attributed_gmv/NULLIF(cost,0),2),ROUND(conversions*1.0/NULLIF(clicks,0),4) FROM dws_campaign_daily""")
    con.commit(); con.close()


def write_metadata() -> None:
    CATALOG_PATH.write_text(json.dumps(CATALOG,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    relations = [{"id":identifier,"from_table":from_table,"from_column":from_column,"to_table":to_table,"to_column":to_column,"relation_type":"foreign_key","cardinality":"many_to_one","recommended_join":"LEFT JOIN","verified":True,"description":description} for identifier,from_table,from_column,to_table,to_column,description in RELATIONS]
    RELATIONS_PATH.write_text(json.dumps({"version":"2.0","relations":relations},ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


if __name__ == "__main__":
    reset_demo_database(); write_metadata(); print(f"Seeded {len(CATALOG['tables'])} catalog tables into {DEMO_DB}")
