"""Persistent LangGraph checkpoint storage kept in the project data directory."""
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver

from backend.core import DATA_DIR

CHECKPOINT_DB = DATA_DIR / "langgraph-checkpoints.db"
_connection = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
checkpoint_saver = SqliteSaver(_connection)
