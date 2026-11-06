"""Run backend tests with throwaway SQLite storage and offline retrieval models."""
import os
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    import backend.core as core
    import pytest
    from cryptography.fernet import Fernet

    # Override paths before importing app/test modules (some legacy tests use db()).
    core.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="backend-tests-", dir=core.DATA_DIR) as directory:
        root = Path(directory)
        core.DATA_DIR = root
        core.PLATFORM_DB = root / "platform.db"
        core.DEMO_DB = root / "demo.db"
        core.db.__defaults__ = (core.PLATFORM_DB,)
        os.environ["EMBEDDING_BACKEND"] = "hash"
        os.environ["RERANK_BACKEND"] = "none"
        os.environ["VECTOR_STORE_BACKEND"] = "sqlite"
        # Worker lifecycle is exercised explicitly with isolated factories.
        os.environ["CATALOG_INDEX_WORKER_ENABLED"] = "0"
        os.environ["PRODUCTION_RECOVERY_MONITOR_ENABLED"] = "0"
        os.environ["APP_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        os.environ.pop("DEEPSEEK_API_KEY", None)
        from backend.evaluation.fixtures import create_warehouse
        from backend.main import init_storage
        create_warehouse(core.DEMO_DB)
        init_storage()
        try:
            return pytest.main(["test", "-q", "--tb=short", "-p", "no:cacheprovider",
                                "--basetemp", str(root / "pytest"), *sys.argv[1:]])
        finally:
            from backend.checkpoint import _connection
            _connection.close()
            # A closed append-mode FileHandler can reopen on a late background
            # log call. Detach owned handlers BEFORE closing/removing temp files.
            app_logger = logging.getLogger('data_agent')
            for handler in list(app_logger.handlers):
                app_logger.removeHandler(handler)
                handler.close()
            logging.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
