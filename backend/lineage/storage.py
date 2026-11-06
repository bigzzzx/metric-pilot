import hashlib
import json
from backend.core import db


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def init_schema(connection_factory=None):
    con = (connection_factory or db)()
    try:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS lineage_sources(
          id TEXT PRIMARY KEY,source_kind TEXT NOT NULL,source_id TEXT,source_uri TEXT NOT NULL,
          source_version TEXT NOT NULL,title TEXT NOT NULL,content TEXT NOT NULL,content_hash TEXT NOT NULL,
          scope_json TEXT NOT NULL,metadata_json TEXT NOT NULL,created_by TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TRIGGER IF NOT EXISTS lineage_sources_immutable_update BEFORE UPDATE ON lineage_sources
          BEGIN SELECT RAISE(ABORT,'Lineage evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS lineage_sources_immutable_delete BEFORE DELETE ON lineage_sources
          BEGIN SELECT RAISE(ABORT,'Lineage evidence is immutable'); END;
        CREATE TABLE IF NOT EXISTS lineage_source_states(
          source_ref TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'active',reason TEXT NOT NULL DEFAULT '',
          version INTEGER NOT NULL DEFAULT 1,analysis_json TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS lineage_candidates(
          id TEXT PRIMARY KEY,source_ref TEXT NOT NULL,kind TEXT NOT NULL,from_asset_id TEXT NOT NULL,
          to_asset_id TEXT NOT NULL,from_column TEXT NOT NULL DEFAULT '',to_column TEXT NOT NULL DEFAULT '',
          expression TEXT NOT NULL,context_json TEXT NOT NULL,locator_json TEXT NOT NULL,
          observation TEXT NOT NULL,schema_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',
          version INTEGER NOT NULL DEFAULT 1,technical_status TEXT NOT NULL DEFAULT 'schema_validated',
          checks_json TEXT NOT NULL DEFAULT '{}',review_json TEXT NOT NULL DEFAULT '{}',reviewed_by TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY(source_ref) REFERENCES lineage_sources(id));
        CREATE INDEX IF NOT EXISTS idx_lineage_target ON lineage_candidates(to_asset_id,to_column,status);
        CREATE INDEX IF NOT EXISTS idx_lineage_source ON lineage_candidates(source_ref,status);
        CREATE TABLE IF NOT EXISTS lineage_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,candidate_id TEXT,source_ref TEXT,actor_id TEXT NOT NULL,
          action TEXT NOT NULL,version INTEGER NOT NULL,details_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS lineage_index_jobs(
          id TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'pending',error TEXT,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,completed_at TEXT);
        CREATE TABLE IF NOT EXISTS lineage_query_history(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,source_id TEXT,sql_text TEXT NOT NULL,
          origin TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        ''')
        from backend.index_worker import migrate
        migrate(con)
        con.commit()
    finally:
        con.close()
