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

    # Override paths before importing app/test modules (some legacy tests use db()).
    with tempfile.TemporaryDirectory(prefix="backend-tests-", dir=core.DATA_DIR) as directory:
        root = Path(directory)
        core.DATA_DIR = root
        core.PLATFORM_DB = root / "platform.db"
        core.DEMO_DB = root / "demo.db"
        core.db.__defaults__ = (core.PLATFORM_DB,)
        os.environ["EMBEDDING_BACKEND"] = "hash"
        os.environ["RERANK_BACKEND"] = "none"
        os.environ["VECTOR_STORE_BACKEND"] = "sqlite"
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
            logging.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
