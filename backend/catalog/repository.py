"""SQLite repository for the governed data catalog.

The catalog is the structured source of truth. RAG consumes only published
records and never becomes the system where definitions or permissions live.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from backend.core import DEMO_DB, ROOT, db

CATALOG_FILE = ROOT / "metadata" / "catalog.json"
RELATIONS_FILE = ROOT / "metadata" / "relations.json"
DISCOVER, VIEW_METADATA, QUERY_DATA, MANAGE = "discover", "view_metadata", "query_data", "manage"
DATA_WRITE, ETL_EXECUTE, TOOL_ADMIN = "data.write", "etl.execute", "tool.admin"
ALL_ACTIONS = (DISCOVER, VIEW_METADATA, QUERY_DATA, MANAGE, DATA_WRITE, ETL_EXECUTE, TOOL_ADMIN)
SENSITIVITY_ORDER = {"public": 0, "internal": 1, "sensitive": 2, "restricted": 3}


def _rows(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


def _ensure_column(con: sqlite3.Connection, table: str, name: str, declaration: str) -> None:
    """Apply a small idempotent SQLite column migration."""
    columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


def init_catalog_schema(connection_factory=None) -> None:
    """Create catalog storage and import the local demo as published data."""
    connection_factory = connection_factory or db
    con = connection_factory()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS catalog_domains(
      id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',owner_group_id TEXT,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS catalog_assets(
      id TEXT PRIMARY KEY,source_id TEXT NOT NULL,namespace TEXT NOT NULL DEFAULT '',physical_name TEXT NOT NULL,display_name TEXT NOT NULL,asset_type TEXT NOT NULL DEFAULT 'table',domain_id TEXT NOT NULL,warehouse_layer TEXT NOT NULL DEFAULT 'UNKNOWN',normalized_stage TEXT NOT NULL DEFAULT 'unknown',description TEXT NOT NULL DEFAULT '',aliases_json TEXT NOT NULL DEFAULT '[]',data_grain TEXT NOT NULL DEFAULT '',primary_key TEXT NOT NULL DEFAULT '',owner_group_id TEXT,sensitivity TEXT NOT NULL DEFAULT 'internal',certification_status TEXT NOT NULL DEFAULT 'draft',lifecycle_status TEXT NOT NULL DEFAULT 'active',publish_status TEXT NOT NULL DEFAULT 'draft',catalog_version INTEGER NOT NULL DEFAULT 1,source_updated_at TEXT,published_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(source_id,namespace,physical_name));
    CREATE TABLE IF NOT EXISTS catalog_columns(
      id TEXT PRIMARY KEY,asset_id TEXT NOT NULL,physical_name TEXT NOT NULL,display_name TEXT NOT NULL,data_type TEXT NOT NULL,ordinal INTEGER NOT NULL,description TEXT NOT NULL DEFAULT '',aliases_json TEXT NOT NULL DEFAULT '[]',semantic_role TEXT NOT NULL DEFAULT 'attribute',unit TEXT NOT NULL DEFAULT '',enum_values_json TEXT NOT NULL DEFAULT '[]',nullable INTEGER NOT NULL DEFAULT 1,is_primary_key INTEGER NOT NULL DEFAULT 0,sensitivity TEXT NOT NULL DEFAULT 'internal',classification TEXT NOT NULL DEFAULT '',masking_strategy TEXT NOT NULL DEFAULT 'none',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(asset_id,physical_name),FOREIGN KEY(asset_id) REFERENCES catalog_assets(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS catalog_relationships(
      id TEXT PRIMARY KEY,from_asset_id TEXT NOT NULL,from_column TEXT NOT NULL,to_asset_id TEXT NOT NULL,to_column TEXT NOT NULL,relation_type TEXT NOT NULL,cardinality TEXT NOT NULL,recommended_join TEXT NOT NULL,join_condition TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',verified INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'published',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS catalog_lineage_nodes(
      id TEXT PRIMARY KEY,node_type TEXT NOT NULL,display_name TEXT NOT NULL,asset_id TEXT,source_id TEXT,description TEXT NOT NULL DEFAULT '',sensitivity TEXT NOT NULL DEFAULT 'internal',published INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS catalog_lineage_edges(
      id TEXT PRIMARY KEY,from_node_id TEXT NOT NULL,to_node_id TEXT NOT NULL,edge_type TEXT NOT NULL,transform_summary TEXT NOT NULL DEFAULT '',confidence REAL NOT NULL DEFAULT 1,status TEXT NOT NULL DEFAULT 'healthy',last_run_at TEXT,published INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS catalog_metrics(
      id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,domain_id TEXT NOT NULL,description TEXT NOT NULL DEFAULT '',definition_sql TEXT NOT NULL,formula TEXT NOT NULL DEFAULT '',grain TEXT NOT NULL DEFAULT '',owner_group_id TEXT,sensitivity TEXT NOT NULL DEFAULT 'internal',certification_status TEXT NOT NULL DEFAULT 'draft',publish_status TEXT NOT NULL DEFAULT 'draft',catalog_version INTEGER NOT NULL DEFAULT 1,published_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS catalog_terms(
      id TEXT PRIMARY KEY,term TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,domain_id TEXT,definition TEXT NOT NULL,aliases_json TEXT NOT NULL DEFAULT '[]',owner_group_id TEXT,sensitivity TEXT NOT NULL DEFAULT 'public',publish_status TEXT NOT NULL DEFAULT 'draft',catalog_version INTEGER NOT NULL DEFAULT 1,published_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS catalog_quality_snapshots(
      id TEXT PRIMARY KEY,asset_id TEXT NOT NULL,score REAL NOT NULL,freshness_at TEXT,completeness REAL,validity REAL,status TEXT NOT NULL DEFAULT 'unknown',details_json TEXT NOT NULL DEFAULT '{}',observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS catalog_quality_runs(
      id TEXT PRIMARY KEY,source_id TEXT NOT NULL,actor_id TEXT,status TEXT NOT NULL DEFAULT 'running',
      asset_count INTEGER NOT NULL DEFAULT 0,healthy_count INTEGER NOT NULL DEFAULT 0,
      warning_count INTEGER NOT NULL DEFAULT 0,critical_count INTEGER NOT NULL DEFAULT 0,
      failed_count INTEGER NOT NULL DEFAULT 0,summary_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,completed_at TEXT);
    CREATE TABLE IF NOT EXISTS catalog_groups(id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS catalog_group_memberships(group_id TEXT NOT NULL,user_id TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(group_id,user_id));
    CREATE TABLE IF NOT EXISTS catalog_roles(id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,display_name TEXT NOT NULL,description TEXT NOT NULL DEFAULT '');
    CREATE TABLE IF NOT EXISTS catalog_user_roles(role_id TEXT NOT NULL,user_id TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(role_id,user_id));
    CREATE TABLE IF NOT EXISTS catalog_policies(id TEXT PRIMARY KEY,subject_type TEXT NOT NULL,subject_id TEXT NOT NULL,resource_type TEXT NOT NULL,resource_id TEXT NOT NULL DEFAULT '*',action TEXT NOT NULL,effect TEXT NOT NULL DEFAULT 'allow',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS catalog_publications(id TEXT PRIMARY KEY,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,version INTEGER NOT NULL,status TEXT NOT NULL,published_by TEXT,summary TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS catalog_audit_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,actor_id TEXT,action TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,detail_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS catalog_sync_runs(
      id TEXT PRIMARY KEY,source_id TEXT NOT NULL,actor_id TEXT,status TEXT NOT NULL DEFAULT 'pending',
      snapshot_version INTEGER NOT NULL,snapshot_hash TEXT NOT NULL,table_count INTEGER NOT NULL DEFAULT 0,
      relationship_count INTEGER NOT NULL DEFAULT 0,added_count INTEGER NOT NULL DEFAULT 0,
      changed_count INTEGER NOT NULL DEFAULT 0,removed_count INTEGER NOT NULL DEFAULT 0,
      relationship_change_count INTEGER NOT NULL DEFAULT 0,pending_count INTEGER NOT NULL DEFAULT 0,
      summary_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      completed_at TEXT);
    CREATE TABLE IF NOT EXISTS catalog_sync_snapshots(
      id TEXT PRIMARY KEY,run_id TEXT NOT NULL,source_id TEXT NOT NULL,version INTEGER NOT NULL,
      snapshot_hash TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(source_id,version));
    CREATE TABLE IF NOT EXISTS catalog_technical_state(
      source_id TEXT NOT NULL,entity_type TEXT NOT NULL,entity_key TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
      content_hash TEXT NOT NULL,payload_json TEXT NOT NULL,updated_by TEXT,
      updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(source_id,entity_type,entity_key));
    CREATE TABLE IF NOT EXISTS catalog_sync_changes(
      id TEXT PRIMARY KEY,run_id TEXT NOT NULL,source_id TEXT NOT NULL,entity_type TEXT NOT NULL,
      entity_key TEXT NOT NULL,asset_id TEXT,change_type TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
      content_hash TEXT NOT NULL,before_json TEXT,after_json TEXT,summary_json TEXT NOT NULL DEFAULT '{}',
      decided_by TEXT,decision_reason TEXT NOT NULL DEFAULT '',decided_at TEXT,
      created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
    CREATE INDEX IF NOT EXISTS idx_catalog_assets_domain ON catalog_assets(domain_id,publish_status);
    CREATE INDEX IF NOT EXISTS idx_catalog_columns_asset ON catalog_columns(asset_id);
    CREATE INDEX IF NOT EXISTS idx_catalog_policy_subject ON catalog_policies(subject_type,subject_id,action);
    CREATE INDEX IF NOT EXISTS idx_catalog_sync_runs_source ON catalog_sync_runs(source_id,created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_catalog_sync_changes_source ON catalog_sync_changes(source_id,status,created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_catalog_quality_runs_source ON catalog_quality_runs(source_id,created_at DESC);
    """)
    _ensure_column(con, "catalog_quality_snapshots", "run_id", "TEXT")
    _ensure_column(con, "catalog_quality_snapshots", "row_count", "INTEGER NOT NULL DEFAULT 0")
    con.execute("CREATE INDEX IF NOT EXISTS idx_catalog_quality_snapshots_run ON catalog_quality_snapshots(run_id)")
    _seed_governance(con)
    _seed_demo_catalog(con)
    from backend.semantic_metrics.storage import migrate
    migrate(con)
    _bootstrap_existing_users(con)
    _cleanup_legacy_local_sync_duplicates(con)
    con.commit(); con.close()


def _cleanup_legacy_local_sync_duplicates(con: sqlite3.Connection) -> None:
    """Remove drafts created when SQLite's implicit `main` namespace was treated as a second catalog."""
    duplicate_ids = [row["id"] for row in con.execute("""
      SELECT draft.id
      FROM catalog_assets draft
      WHERE draft.source_id='local-ecommerce-demo'
        AND draft.namespace='main'
        AND draft.publish_status='draft'
        AND EXISTS (
          SELECT 1 FROM catalog_assets published
          WHERE published.source_id=draft.source_id
            AND published.namespace=''
            AND published.physical_name=draft.physical_name
            AND published.publish_status='published'
        )
    """)]
    if not duplicate_ids:
        return
    holders = ",".join("?" for _ in duplicate_ids)
    con.execute(
        f"DELETE FROM catalog_relationships WHERE from_asset_id IN ({holders}) OR to_asset_id IN ({holders})",
        [*duplicate_ids, *duplicate_ids],
    )
    con.execute(f"DELETE FROM catalog_columns WHERE asset_id IN ({holders})", duplicate_ids)
    con.execute(f"DELETE FROM catalog_assets WHERE id IN ({holders})", duplicate_ids)


def _seed_governance(con: sqlite3.Connection) -> None:
    con.execute(
        "INSERT OR IGNORE INTO catalog_domains(id,name,display_name,description,owner_group_id) VALUES (?,?,?,?,?)",
        ("domain-supply-chain", "supply-chain", "供应链域", "商品、供应商、库存及履约相关数据资产。", "group-catalog-stewards"),
    )
    for row in [
        ("group-ecommerce-analysts", "ecommerce-analysts", "电商数据分析组", "本地演示数据域的默认分析人员。"),
        ("group-catalog-stewards", "catalog-stewards", "数据目录管理员", "负责目录审核、发布与权限治理。"),
        ("group-marketing-analysts", "marketing-analysts", "营销数据分析组", "可访问营销业务域内的已授权数据资产。"),
        ("group-supply-chain-analysts", "supply-chain-analysts", "供应链数据分析组", "可访问供应链业务域内的已授权数据资产。"),
    ]: con.execute("INSERT OR IGNORE INTO catalog_groups VALUES (?,?,?,?)", row)
    for row in [
        ("role-platform-admin", "platform-admin", "平台管理员", "可管理所有目录、权限、数据源与发布。"),
        ("role-data-analyst", "data-analyst", "数据分析师", "可发现、查看和查询已授权的数据资产。"),
        ("role-data-steward", "data-steward", "数据管理员", "可维护目录语义、发布资产和治理策略，但不能管理平台用户或数据源。"),
        ("role-data-engineer", "data-engineer", "数据工程师", "可在人工审批后执行数据写入和已登记的 ETL 任务。"),
    ]: con.execute("INSERT OR IGNORE INTO catalog_roles VALUES (?,?,?,?)", row)
    for row in [
        ("policy-admin", "role", "role-platform-admin", "platform", "*", "*", "allow"),
        ("policy-steward-manage", "role", "role-data-steward", "platform", "*", MANAGE, "allow"),
        ("policy-engineer-write", "role", "role-data-engineer", "platform", "*", DATA_WRITE, "allow"),
        ("policy-engineer-etl", "role", "role-data-engineer", "platform", "*", ETL_EXECUTE, "allow"),
        ("policy-marketing-discover", "group", "group-marketing-analysts", "domain", "domain-marketing", DISCOVER, "allow"),
        ("policy-marketing-metadata", "group", "group-marketing-analysts", "domain", "domain-marketing", VIEW_METADATA, "allow"),
        ("policy-marketing-query", "group", "group-marketing-analysts", "domain", "domain-marketing", QUERY_DATA, "allow"),
        ("policy-supply-discover", "group", "group-supply-chain-analysts", "domain", "domain-supply-chain", DISCOVER, "allow"),
        ("policy-supply-metadata", "group", "group-supply-chain-analysts", "domain", "domain-supply-chain", VIEW_METADATA, "allow"),
        ("policy-supply-query", "group", "group-supply-chain-analysts", "domain", "domain-supply-chain", QUERY_DATA, "allow"),
        ("policy-analyst-discover", "group", "group-ecommerce-analysts", "domain", "domain-ecommerce", DISCOVER, "allow"),
        ("policy-analyst-metadata", "group", "group-ecommerce-analysts", "domain", "domain-ecommerce", VIEW_METADATA, "allow"),
        ("policy-analyst-query", "group", "group-ecommerce-analysts", "domain", "domain-ecommerce", QUERY_DATA, "allow"),
    ]: con.execute("INSERT OR IGNORE INTO catalog_policies(id,subject_type,subject_id,resource_type,resource_id,action,effect) VALUES (?,?,?,?,?,?,?)", row)


def _demo_column_policy(table: str, column: str) -> tuple[str, str, str]:
    if column in {"name", "user_name"}: return "sensitive", "person_name", "partial"
    if column in {"user_id", "city"}: return "internal", "personal_or_quasi_identifier", "none"
    return "internal", "", "none"


def _seed_demo_catalog(con: sqlite3.Connection) -> None:
    """Add missing demo metadata without changing existing catalog enrichment."""
    if not CATALOG_FILE.exists(): return
    catalog = json.loads(CATALOG_FILE.read_text(encoding="utf-8")); source_id = str(catalog.get("source", {}).get("id") or "local-ecommerce-demo")
    for domain in catalog.get("domains", []):
        con.execute("INSERT OR IGNORE INTO catalog_domains(id,name,display_name,description,owner_group_id) VALUES (?,?,?,?,?)", (domain["id"],domain["name"],domain["display_name"],domain.get("description", ""),"group-catalog-stewards"))
    for table in catalog.get("tables", []):
        name = table["name"]; asset_id = f"asset-{name}"
        con.execute("""INSERT OR IGNORE INTO catalog_assets(id,source_id,physical_name,display_name,asset_type,domain_id,warehouse_layer,normalized_stage,description,aliases_json,data_grain,primary_key,owner_group_id,sensitivity,certification_status,publish_status,published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'published',CURRENT_TIMESTAMP)""", (asset_id,source_id,name,table.get("display_name") or name,"table",table.get("domain_id", "domain-ecommerce"),table.get("warehouse_layer", "UNKNOWN"),table.get("normalized_stage", "unknown"),table.get("description", ""),json.dumps(table.get("aliases", []),ensure_ascii=False),table.get("data_grain", ""),table.get("primary_key", ""),"group-ecommerce-analysts",table.get("sensitivity", "internal"),"certified" if table.get("warehouse_layer") in {"DWS","ADS"} else "validated"))
        for ordinal, item in enumerate(table.get("columns", []), 1):
            sensitivity, classification, masking = _demo_column_policy(name, item["name"])
            con.execute("""INSERT OR IGNORE INTO catalog_columns(id,asset_id,physical_name,display_name,data_type,ordinal,description,aliases_json,semantic_role,is_primary_key,sensitivity,classification,masking_strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", (f"column-{name}-{item['name']}",asset_id,item["name"],item.get("display_name") or item["name"],item.get("type", "TEXT"),ordinal,item.get("description", ""),json.dumps(item.get("aliases", []),ensure_ascii=False),item.get("semantic_role", "attribute"),int(item["name"] == table.get("primary_key")),sensitivity,classification,masking))
    if RELATIONS_FILE.exists():
        for relation in json.loads(RELATIONS_FILE.read_text(encoding="utf-8")).get("relations", []):
            con.execute("""INSERT OR IGNORE INTO catalog_relationships(id,from_asset_id,from_column,to_asset_id,to_column,relation_type,cardinality,recommended_join,join_condition,description,verified) VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (relation["id"],f"asset-{relation['from_table']}",relation["from_column"],f"asset-{relation['to_table']}",relation["to_column"],relation.get("relation_type", "foreign_key"),relation.get("cardinality", "many_to_one"),relation.get("recommended_join", "LEFT JOIN"),f"{relation['from_table']}.{relation['from_column']} = {relation['to_table']}.{relation['to_column']}",relation.get("description", ""),int(relation.get("verified",False))))
    _seed_demo_lineage_metrics(con, catalog.get("tables", []))


def _seed_demo_lineage_metrics(con: sqlite3.Connection, tables: list[dict]) -> None:
    source_id = "local-ecommerce-demo"
    for node in [("lineage-source-ecommerce","source_system","电商业务源系统",None,source_id,"用户、商品、订单、支付的源系统。","internal"),("lineage-source-marketing","source_system","营销投放源系统",None,source_id,"广告投放与营销触点源系统。","internal")]: con.execute("INSERT OR IGNORE INTO catalog_lineage_nodes(id,node_type,display_name,asset_id,source_id,description,sensitivity) VALUES (?,?,?,?,?,?,?)", node)
    names = {item["name"] for item in tables}
    for item in tables:
        name=item["name"]; domain=item.get("domain_id", "domain-ecommerce"); node_id=f"lineage-asset-{name}"
        con.execute("INSERT OR IGNORE INTO catalog_lineage_nodes(id,node_type,display_name,asset_id,source_id,description,sensitivity) VALUES (?,?,?,?,?,?,?)", (node_id,"table",item["display_name"],f"asset-{name}",source_id,"已发布数据目录资产。",item.get("sensitivity","internal")))
        if item.get("warehouse_layer") == "ODS":
            upstream = "lineage-source-ecommerce" if domain == "domain-ecommerce" else "lineage-source-marketing"
            con.execute("INSERT OR IGNORE INTO catalog_lineage_edges(id,from_node_id,to_node_id,edge_type,transform_summary) VALUES (?,?,?,?,?)", (f"lineage-{name}-ingest",upstream,node_id,"ingests","从源系统采集原始业务事件"))
    transforms = [("ods_user_snapshot","users"),("ods_product_snapshot","products"),("ods_order_events","orders"),("ods_order_events","dwd_payment_detail"),("orders","dws_daily_sales"),("users","dws_daily_sales"),("products","dws_daily_sales"),("orders","dws_user_purchase_30d"),("dws_daily_sales","ads_sales_dashboard"),("ods_campaign_events","dwd_campaign_attribution"),("orders","dwd_campaign_attribution"),("ods_campaign_events","dws_campaign_daily"),("dwd_campaign_attribution","dws_campaign_daily"),("dws_campaign_daily","ads_campaign_roi")]
    for source,target in transforms:
        if source in names and target in names: con.execute("INSERT OR IGNORE INTO catalog_lineage_edges(id,from_node_id,to_node_id,edge_type,transform_summary) VALUES (?,?,?,?,?)", (f"lineage-{source}-to-{target}",f"lineage-asset-{source}",f"lineage-asset-{target}","transforms","清洗、关联或聚合生成下游数据资产"))
    metrics = [("metric-roi","roi","营销 ROI","domain-marketing","归因 GMV 相对于投放成本的回报率","SUM(attributed_gmv) / SUM(cost)","归因 GMV 除以投放成本。","按日期、活动、渠道聚合","certified"),("metric-conversion-rate","conversion_rate","转化率","domain-marketing","点击后完成转化的比例","SUM(conversions) / SUM(clicks)","归因转化数除以点击数。","按日期、活动、渠道聚合","validated")]
    for metric in metrics: con.execute("INSERT OR IGNORE INTO catalog_metrics(id,name,display_name,domain_id,description,definition_sql,formula,grain,owner_group_id,sensitivity,certification_status,publish_status,published_at) VALUES (?,?,?,?,?,?,?,?,?,'internal',?,'published',CURRENT_TIMESTAMP)", (metric[0],metric[1],metric[2],metric[3],metric[4],metric[5],metric[6],metric[7],"group-ecommerce-analysts",metric[8]))
    terms = [("term-roi","roi","营销 ROI","domain-marketing","归因 GMV 与投放成本的比值。",["投产比","回报率"]),("term-attribution","attribution","营销归因","domain-marketing","将用户转化结果分配给营销触点的规则。",["转化归因"])]
    for term in terms: con.execute("INSERT OR IGNORE INTO catalog_terms(id,term,display_name,domain_id,definition,aliases_json,owner_group_id,sensitivity,publish_status,published_at) VALUES (?,?,?,?,?,?,?,'public','published',CURRENT_TIMESTAMP)", (term[0],term[1],term[2],term[3],term[4],json.dumps(term[5],ensure_ascii=False),"group-ecommerce-analysts"))
    for index,item in enumerate(tables): con.execute("INSERT OR IGNORE INTO catalog_quality_snapshots(id,asset_id,score,freshness_at,completeness,validity,status,details_json) VALUES (?,?,?,?,?,?,?,?)", (f"quality-{item['name']}",f"asset-{item['name']}",round(.96+(index%4)*.01,2),"2026-08-26T00:00:00Z",.99,.98,"healthy",json.dumps({"source":"local-demo","checks":["freshness","null_rate","schema"]})))

def _bootstrap_existing_users(con: sqlite3.Connection) -> None:
    try: users = con.execute("SELECT id FROM users ORDER BY created_at,id").fetchall()
    except sqlite3.OperationalError: return
    for row in users:
        con.execute("INSERT OR IGNORE INTO catalog_group_memberships(group_id,user_id) VALUES (?,?)", ("group-ecommerce-analysts",row["id"]))
        con.execute("INSERT OR IGNORE INTO catalog_user_roles(role_id,user_id) VALUES (?,?)", ("role-data-analyst",row["id"]))
    if users and not con.execute("SELECT 1 FROM catalog_user_roles WHERE role_id='role-platform-admin'").fetchone(): con.execute("INSERT OR IGNORE INTO catalog_user_roles(role_id,user_id) VALUES (?,?)", ("role-platform-admin",users[0]["id"]))


def provision_user_access(user_id: str) -> None:
    con = db(); con.execute("INSERT OR IGNORE INTO catalog_group_memberships(group_id,user_id) VALUES (?,?)", ("group-ecommerce-analysts",user_id)); con.execute("INSERT OR IGNORE INTO catalog_user_roles(role_id,user_id) VALUES (?,?)", ("role-data-analyst",user_id))
    if not con.execute("SELECT 1 FROM catalog_user_roles WHERE role_id='role-platform-admin'").fetchone(): con.execute("INSERT OR IGNORE INTO catalog_user_roles(role_id,user_id) VALUES (?,?)", ("role-platform-admin",user_id))
    con.commit(); con.close()
