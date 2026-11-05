"""Actual app in a disposable workspace for the data-service browser acceptance."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    import backend.core as core
    from cryptography.fernet import Fernet
    with tempfile.TemporaryDirectory(prefix='metric-pilot-ui-') as directory:
        core.DATA_DIR = Path(directory)
        core.PLATFORM_DB = core.DATA_DIR / 'platform.db'
        core.DEMO_DB = core.DATA_DIR / 'warehouse.db'
        core.db.__defaults__ = (core.PLATFORM_DB,)
        os.environ.update(APP_ENCRYPTION_KEY=Fernet.generate_key().decode(), EMBEDDING_BACKEND='hash',
            VECTOR_STORE_BACKEND='sqlite', RERANK_BACKEND='none', MEMORY_VECTOR_BACKEND='disabled')
        os.environ.pop('DEEPSEEK_API_KEY',None)
        from backend.evaluation.fixtures import create_warehouse
        from backend.main import app, init_storage
        create_warehouse(core.DEMO_DB)
        init_storage()
        con = core.db()
        physical = core.db(core.DEMO_DB)
        names = {row['name'] for row in physical.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for asset in con.execute('SELECT id,physical_name FROM catalog_assets').fetchall():
            if asset['physical_name'] not in names:
                con.execute("UPDATE catalog_assets SET publish_status='draft' WHERE id=?", (asset['id'],))
                continue
            columns = {row['name'] for row in physical.execute('PRAGMA table_info("'+asset['physical_name']+'")')}
            for column in con.execute('SELECT id,physical_name FROM catalog_columns WHERE asset_id=?',(asset['id'],)).fetchall():
                if column['physical_name'] not in columns:
                    con.execute('DELETE FROM catalog_columns WHERE id=?',(column['id'],))
        con.commit(); con.close(); physical.close()
        from backend.evaluation.lineage import prepare_lineage_fixture
        prepare_lineage_fixture(core.db,core.DEMO_DB)
        import uvicorn
        try:
            uvicorn.run(app, host='127.0.0.1', port=3302)
        finally:
            from backend.checkpoint import _connection
            _connection.close()


if __name__ == '__main__':
    main()
