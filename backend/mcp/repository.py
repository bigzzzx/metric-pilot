"""SQLite repository for MCP servers and their discovered tools."""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Callable

from backend.core import db, secrets

ConnectionFactory = Callable[[], Any]


def _id_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def init_mcp_schema(connection: Any) -> None:
    server_columns = {row["name"] for row in connection.execute("PRAGMA table_info(mcp_servers)")}
    additions = {
        "description": "TEXT NOT NULL DEFAULT ''",
        "auth_type": "TEXT NOT NULL DEFAULT 'bearer'",
        "timeout_seconds": "INTEGER NOT NULL DEFAULT 30",
        "protocol_version": "TEXT NOT NULL DEFAULT ''",
        "capabilities": "TEXT NOT NULL DEFAULT '{}'",
        "last_synced_at": "TEXT",
        "updated_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    }
    for name, declaration in additions.items():
        if name not in server_columns:
            connection.execute(f"ALTER TABLE mcp_servers ADD COLUMN {name} {declaration}")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS mcp_tools(
            id TEXT PRIMARY KEY,
            server_id TEXT NOT NULL,
            name TEXT NOT NULL,
            display_name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            input_schema TEXT NOT NULL DEFAULT '{}',
            output_schema TEXT NOT NULL DEFAULT '{}',
            annotations TEXT NOT NULL DEFAULT '{}',
            execution TEXT NOT NULL DEFAULT '{}',
            enabled INTEGER NOT NULL DEFAULT 1,
            risk_level TEXT NOT NULL DEFAULT 'medium',
            required_permission TEXT NOT NULL DEFAULT '',
            schema_hash TEXT NOT NULL DEFAULT '',
            unavailable INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT,
            last_call_status TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(server_id, name),
            FOREIGN KEY(server_id) REFERENCES mcp_servers(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_mcp_tools_server ON mcp_tools(server_id);
        """
    )
    agent_columns = {row["name"] for row in connection.execute("PRAGMA table_info(agents)")}
    if "mcp_tools" not in agent_columns:
        connection.execute("ALTER TABLE agents ADD COLUMN mcp_tools TEXT DEFAULT '[]'")


def _server_view(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"] or "",
        "transport": row["transport"],
        "endpoint": row["endpoint"],
        "auth_type": row["auth_type"],
        "credential_configured": bool(row["api_key"]),
        "timeout_seconds": row["timeout_seconds"],
        "enabled": bool(row["enabled"]),
        "protocol_version": row["protocol_version"] or "",
        "capabilities": json.loads(row["capabilities"] or "{}"),
        "last_tested_at": row["last_tested_at"],
        "last_test_status": row["last_test_status"],
        "last_synced_at": row["last_synced_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _tool_view(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "server_id": row["server_id"],
        "name": row["name"],
        "display_name": row["display_name"],
        "description": row["description"] or "",
        "input_schema": json.loads(row["input_schema"] or "{}"),
        "output_schema": json.loads(row["output_schema"] or "{}"),
        "annotations": json.loads(row["annotations"] or "{}"),
        "execution": json.loads(row["execution"] or "{}"),
        "enabled": bool(row["enabled"]),
        "risk_level": row["risk_level"],
        "required_permission": row["required_permission"] or "",
        "unavailable": bool(row["unavailable"]),
        "last_seen_at": row["last_seen_at"],
        "last_call_status": row["last_call_status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_servers(connection_factory: ConnectionFactory = db) -> list[dict[str, Any]]:
    connection = connection_factory()
    try:
        rows = connection.execute("SELECT * FROM mcp_servers ORDER BY created_at DESC").fetchall()
        counts = {row["server_id"]: row["count"] for row in connection.execute(
            "SELECT server_id,COUNT(*) AS count FROM mcp_tools WHERE unavailable=0 GROUP BY server_id"
        )}
        return [{**_server_view(row), "tool_count": counts.get(row["id"], 0)} for row in rows]
    finally:
        connection.close()


def get_server(
    server_id: str,
    *,
    include_secret: bool = False,
    connection_factory: ConnectionFactory = db,
) -> dict[str, Any] | None:
    connection = connection_factory()
    try:
        row = connection.execute("SELECT * FROM mcp_servers WHERE id=?", (server_id,)).fetchone()
        if not row:
            return None
        result = _server_view(row)
        if include_secret:
            result["api_key"] = secrets().decrypt(row["api_key"].encode()).decode() if row["api_key"] else ""
        return result
    finally:
        connection.close()


def create_server(
    *,
    name: str,
    description: str,
    transport: str,
    endpoint: str,
    auth_type: str,
    api_key: str,
    timeout_seconds: int,
    enabled: bool,
    connection_factory: ConnectionFactory = db,
) -> dict[str, Any]:
    identifier = str(uuid.uuid4())
    encrypted = secrets().encrypt(api_key.encode()).decode() if api_key else ""
    connection = connection_factory()
    try:
        connection.execute(
            """INSERT INTO mcp_servers(
                id,name,description,transport,endpoint,auth_type,api_key,timeout_seconds,enabled
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                identifier, name.strip(), description.strip(), transport, endpoint.rstrip("/"),
                auth_type, encrypted, timeout_seconds, int(enabled),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return get_server(identifier, connection_factory=connection_factory) or {}


def update_server(server_id: str, values: dict[str, Any], connection_factory: ConnectionFactory = db) -> dict[str, Any] | None:
    connection = connection_factory()
    try:
        existing = connection.execute("SELECT * FROM mcp_servers WHERE id=?", (server_id,)).fetchone()
        if not existing:
            return None
        encrypted = existing["api_key"]
        if values.get("api_key"):
            encrypted = secrets().encrypt(str(values["api_key"]).encode()).decode()
        connection.execute(
            """UPDATE mcp_servers SET name=?,description=?,transport=?,endpoint=?,auth_type=?,
               api_key=?,timeout_seconds=?,enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (
                values["name"].strip(), values.get("description", "").strip(), values["transport"],
                values["endpoint"].rstrip("/"), values.get("auth_type", "bearer"), encrypted,
                values.get("timeout_seconds", 30), int(values.get("enabled", True)), server_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return get_server(server_id, connection_factory=connection_factory)


def delete_server(
    server_id: str, connection_factory: ConnectionFactory = db
) -> dict[str, Any] | None:
    """Delete a server and remove all stale Agent and runtime identifiers."""
    connection = connection_factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        server = connection.execute(
            "SELECT id,name FROM mcp_servers WHERE id=?", (server_id,)
        ).fetchone()
        if server is None:
            connection.rollback()
            return None
        tool_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM mcp_tools WHERE server_id=?", (server_id,)
            ).fetchall()
        ]
        tool_set = set(tool_ids)
        affected_agents = 0
        for agent in connection.execute(
            "SELECT id,mcp_servers,mcp_tools FROM agents"
        ).fetchall():
            servers = _id_list(agent["mcp_servers"])
            tools = _id_list(agent["mcp_tools"])
            next_servers = [item for item in servers if item != server_id]
            next_tools = [item for item in tools if item not in tool_set]
            if next_servers != servers or next_tools != tools:
                connection.execute(
                    "UPDATE agents SET mcp_servers=?,mcp_tools=? WHERE id=?",
                    (
                        json.dumps(next_servers, ensure_ascii=False),
                        json.dumps(next_tools, ensure_ascii=False),
                        agent["id"],
                    ),
                )
                affected_agents += 1
        connection.execute("DELETE FROM mcp_tools WHERE server_id=?", (server_id,))
        connection.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
        connection.commit()
        return {
            "id": server_id,
            "name": server["name"],
            "tool_ids": tool_ids,
            "deleted_tool_count": len(tool_ids),
            "updated_agent_count": affected_agents,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_server_status(
    server_id: str,
    status: str,
    *,
    protocol_version: str = "",
    capabilities: dict[str, Any] | None = None,
    synced: bool = False,
    connection_factory: ConnectionFactory = db,
) -> None:
    connection = connection_factory()
    try:
        connection.execute(
            """UPDATE mcp_servers SET last_tested_at=CURRENT_TIMESTAMP,last_test_status=?,
               protocol_version=COALESCE(NULLIF(?,''),protocol_version),
               capabilities=CASE WHEN ?='' THEN capabilities ELSE ? END,
               last_synced_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE last_synced_at END,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (
                status, protocol_version,
                "" if capabilities is None else "set", json.dumps(capabilities or {}, ensure_ascii=False),
                int(synced), server_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _risk_from_annotations(annotations: dict[str, Any]) -> str:
    if annotations.get("readOnlyHint") is True:
        return "low"
    if annotations.get("destructiveHint") is True:
        return "high"
    return "medium"


def replace_discovered_tools(
    server_id: str,
    tools: list[dict[str, Any]],
    connection_factory: ConnectionFactory = db,
) -> list[dict[str, Any]]:
    connection = connection_factory()
    try:
        seen: list[str] = []
        for tool in tools:
            name = str(tool["name"])
            identifier = f"mcp:{server_id}:{name}"
            seen.append(name)
            annotations = tool.get("annotations") or {}
            input_schema = tool.get("inputSchema") or tool.get("input_schema") or {"type": "object"}
            output_schema = tool.get("outputSchema") or tool.get("output_schema") or {}
            execution = tool.get("execution") or {}
            schema_hash = hashlib.sha256(json.dumps(
                {"input": input_schema, "output": output_schema}, sort_keys=True, ensure_ascii=False,
            ).encode()).hexdigest()
            connection.execute(
                """INSERT INTO mcp_tools(
                    id,server_id,name,display_name,description,input_schema,output_schema,
                    annotations,execution,enabled,risk_level,schema_hash,unavailable,last_seen_at
                ) VALUES (?,?,?,?,?,?,?,?,?,1,?,?,0,CURRENT_TIMESTAMP)
                ON CONFLICT(server_id,name) DO UPDATE SET
                    display_name=excluded.display_name,description=excluded.description,
                    input_schema=excluded.input_schema,output_schema=excluded.output_schema,
                    annotations=excluded.annotations,execution=excluded.execution,
                    schema_hash=excluded.schema_hash,unavailable=0,last_seen_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP""",
                (
                    identifier, server_id, name,
                    tool.get("title") or annotations.get("title") or name,
                    tool.get("description") or "",
                    json.dumps(input_schema, ensure_ascii=False),
                    json.dumps(output_schema, ensure_ascii=False),
                    json.dumps(annotations, ensure_ascii=False),
                    json.dumps(execution, ensure_ascii=False),
                    _risk_from_annotations(annotations), schema_hash,
                ),
            )
        if seen:
            placeholders = ",".join("?" for _ in seen)
            connection.execute(
                f"UPDATE mcp_tools SET unavailable=1,updated_at=CURRENT_TIMESTAMP WHERE server_id=? AND name NOT IN ({placeholders})",
                (server_id, *seen),
            )
        else:
            connection.execute(
                "UPDATE mcp_tools SET unavailable=1,updated_at=CURRENT_TIMESTAMP WHERE server_id=?",
                (server_id,),
            )
        connection.commit()
    finally:
        connection.close()
    return list_tools(server_id, connection_factory=connection_factory)


def list_tools(server_id: str | None = None, connection_factory: ConnectionFactory = db) -> list[dict[str, Any]]:
    connection = connection_factory()
    try:
        if server_id:
            rows = connection.execute(
                "SELECT * FROM mcp_tools WHERE server_id=? ORDER BY display_name", (server_id,)
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM mcp_tools ORDER BY server_id,display_name").fetchall()
        return [_tool_view(row) for row in rows]
    finally:
        connection.close()


def get_tool(tool_id: str, connection_factory: ConnectionFactory = db) -> dict[str, Any] | None:
    connection = connection_factory()
    try:
        row = connection.execute("SELECT * FROM mcp_tools WHERE id=?", (tool_id,)).fetchone()
        return _tool_view(row) if row else None
    finally:
        connection.close()


def update_tool(tool_id: str, values: dict[str, Any], connection_factory: ConnectionFactory = db) -> dict[str, Any] | None:
    connection = connection_factory()
    try:
        existing = connection.execute("SELECT * FROM mcp_tools WHERE id=?", (tool_id,)).fetchone()
        if not existing:
            return None
        connection.execute(
            """UPDATE mcp_tools SET enabled=?,risk_level=?,required_permission=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (
                int(values.get("enabled", bool(existing["enabled"]))),
                values.get("risk_level", existing["risk_level"]),
                values.get("required_permission", existing["required_permission"]),
                tool_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return get_tool(tool_id, connection_factory=connection_factory)


def record_tool_call(tool_id: str, status: str, connection_factory: ConnectionFactory = db) -> None:
    connection = connection_factory()
    try:
        connection.execute(
            "UPDATE mcp_tools SET last_call_status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (status[:300], tool_id),
        )
        connection.commit()
    finally:
        connection.close()
