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
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if path == PLATFORM_DB:
        connection.execute("PRAGMA journal_mode = WAL")
    return connection


def secrets() -> Fernet:
    key = os.environ.get("APP_ENCRYPTION_KEY")
    if not key:
        # Local desktop runs may be launched outside the shell that exported
        # environment variables. Keep the secret in the ignored .env file,
        # but make the development server independent from its parent shell.
        env_file = ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                name, separator, value = line.partition("=")
                if separator and name.strip() == "APP_ENCRYPTION_KEY":
                    key = value.strip()
                    break
    if not key:
        raise RuntimeError("APP_ENCRYPTION_KEY is required")
    return Fernet(key.encode())
