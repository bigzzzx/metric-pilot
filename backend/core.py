"""Shared paths, SQLite connections, and encrypted-secret helpers."""
import os
import sqlite3
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / ".data"
DATA_DIR.mkdir(exist_ok=True)
PLATFORM_DB = DATA_DIR / "platform.db"
DEMO_DB = DATA_DIR / "demo.db"


def db(path: Path = PLATFORM_DB) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def secrets() -> Fernet:
    key = os.environ.get("APP_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("APP_ENCRYPTION_KEY is required")
    return Fernet(key.encode())
