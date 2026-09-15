"""Versioned metric contracts and reviewed Wiki projections in the catalog."""
import json


def migrate(con):
    columns = {row['name'] for row in con.execute('PRAGMA table_info(catalog_metrics)')}
    for name, default in [('aliases_json', '[]'), ('contract_json', '{}'), ('wiki_json', '{}'), ('wiki_draft_json', '{}')]:
        if name not in columns:
            con.execute(f"ALTER TABLE catalog_metrics ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
    con.execute('''CREATE TABLE IF NOT EXISTS metric_wiki_revisions(
        metric_id TEXT NOT NULL, version INTEGER NOT NULL, snapshot_json TEXT NOT NULL,
        actor_id TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(metric_id,version))''')
    # Separate, explicitly named synthetic definitions never overwrite existing metrics.
    if not con.execute("SELECT 1 FROM catalog_assets WHERE id='asset-orders'").fetchone():
        return
    for suffix, title, filters, expression in [
        ('paid', '演示已支付金额', [{'column': 'status', 'operator': '=', 'value': 'PAID'}], 'SUM(amount)'),
        ('booked', '演示下单金额', [], 'SUM(amount)'),
    ]:
        identifier = 'metric-demo-' + suffix
        contract = dict(source_id='local-ecommerce-demo', asset_id='asset-orders', namespace='', table='orders',
                        dialect='sqlite', measure_expression=expression, measure_alias=suffix+'_amount',
                        time_column='order_date', requires_time_range=True, required_filters=filters,
                        dimensions={'date': dict(column='order_date', label='日期', aliases=['天', '日'], kind='time'),
                                    'product': dict(column='product_id', label='商品', aliases=['产品'])})
        description = ('仅统计 PAID 状态订单金额，排除取消和退款状态。' if filters else '统计全部状态的下单金额，包括后续取消或退款订单。')
        wiki = dict(summary=description, content=title+'：'+description+' 使用订单日期 order_date，不代表支付日期。'
                    +'“演示销售额”存在两种口径，必须先选择演示已支付金额或演示下单金额。',
                    related_metric_ids=['metric-demo-booked' if suffix=='paid' else 'metric-demo-paid'],
                    source_refs=['catalog:asset:asset-orders'], status='approved', origin='reviewed-demo')
        con.execute('''INSERT OR IGNORE INTO catalog_metrics(id,name,display_name,domain_id,description,
            definition_sql,formula,grain,certification_status,publish_status,aliases_json,contract_json,wiki_json)
            VALUES (?,?,?,'domain-ecommerce',?,?,?,'订单日期','certified','published',?,?,?)''',
            (identifier, 'demo_'+suffix+'_amount', title, description, expression, description,
             json.dumps([title,'演示销售额'],ensure_ascii=False), json.dumps(contract,ensure_ascii=False),
             json.dumps(wiki,ensure_ascii=False)))


def metric_document(row):
    item = dict(row)
    contract = json.loads(item.get('contract_json') or '{}')
    return dict(id='catalog:metric:'+item['id'], kind='metric', title='指标：'+item['display_name'],
                content=(f"指标：{item['display_name']}。定义：{item['description']}。公式：{item['formula']}。"
                         f"SQL 口径：{item['definition_sql']}。粒度：{item['grain']}。认证状态：{item['certification_status']}。"
                         f"版本：{item['catalog_version']}。执行契约：{item.get('contract_json','{}')}。"),
                keywords=[item['name'],item['display_name'],*json.loads(item.get('aliases_json') or '[]'),'指标','口径'],
                domain_id=item['domain_id'], asset_ids=[contract['asset_id']] if contract.get('asset_id') else [],
                source_id=contract.get('source_id'), source_kind=None)


def wiki_document(row):
    item = dict(row)
    wiki = json.loads(item.get('wiki_json') or '{}')
    if wiki.get('status') != 'approved':
        return None
    doc = metric_document(row)
    return {**doc, 'id': 'catalog:metric-wiki:'+item['id'], 'kind':'metric_wiki',
            'title': item['display_name']+' · 口径 Wiki',
            'content': str(wiki.get('summary',''))+'\n'+str(wiki.get('content','')),
            'metric_id':item['id']}
