"""Small durable service audit and production-run tables."""
from backend.core import db


def init_schema(connection_factory=None):
    con = (connection_factory or db)()
    try:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS metric_service_queries(
          id TEXT PRIMARY KEY,user_id TEXT NOT NULL,metric_id TEXT NOT NULL,metric_version TEXT NOT NULL,
          status TEXT NOT NULL,row_count INTEGER NOT NULL DEFAULT 0,duration_ms REAL NOT NULL,
          error_code TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE INDEX IF NOT EXISTS idx_metric_service_user_time ON metric_service_queries(user_id,created_at);
        CREATE TABLE IF NOT EXISTS production_jobs(
          id TEXT PRIMARY KEY,name TEXT NOT NULL,source_asset_id TEXT NOT NULL,target_asset_id TEXT NOT NULL UNIQUE,
          config_json TEXT NOT NULL,version TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,
          created_by TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS production_runs(
          id TEXT PRIMARY KEY,job_id TEXT NOT NULL,job_version TEXT NOT NULL,actor_id TEXT NOT NULL,
          request_key TEXT NOT NULL,arguments_hash TEXT NOT NULL,status TEXT NOT NULL,
          start_date TEXT NOT NULL,end_date TEXT NOT NULL,steps_json TEXT NOT NULL DEFAULT '[]',
          result_json TEXT NOT NULL DEFAULT '{}',error_code TEXT,error_message TEXT,
          started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,completed_at TEXT,
          UNIQUE(actor_id,request_key));
        CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_production_job
          ON production_runs(job_id) WHERE status IN ('running','needs_reconciliation');
        CREATE TABLE IF NOT EXISTS asset_materializations(
          asset_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,data_version INTEGER NOT NULL DEFAULT 0,
          data_updated_at TEXT NOT NULL,range_start TEXT NOT NULL,range_end TEXT NOT NULL);
        ''')
        con.commit()
    finally:
        con.close()
