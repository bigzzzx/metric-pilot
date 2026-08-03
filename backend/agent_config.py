"""Runtime Agent configuration loaded from the management database."""
import json

from fastapi import HTTPException

from backend.core import db


def get_agent(agent_id: str) -> dict:
    connection = db()
    row = connection.execute("SELECT * FROM agents WHERE id=? AND enabled=1", (agent_id,)).fetchone()
    connection.close()
    if not row:
        raise HTTPException(404, f"Agent configuration not found: {agent_id}")
    item = dict(row)
    item["tools"] = json.loads(item["tools"])
    return item


def has_tool(agent_id: str, tool: str) -> bool:
    return tool in get_agent(agent_id)["tools"]
