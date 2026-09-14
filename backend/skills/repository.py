"""Persistent, versioned Skill repository."""
from __future__ import annotations

import json
import uuid
from typing import Any, Callable

from backend.core import db

ConnectionFactory = Callable[[], Any]


def _id_list(raw: Any) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def init_skill_schema(connection: Any) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(skills)")}
    additions = {
        "applicability": "TEXT NOT NULL DEFAULT ''",
        "tags": "TEXT NOT NULL DEFAULT '[]'",
        "examples": "TEXT NOT NULL DEFAULT '[]'",
        "priority": "INTEGER NOT NULL DEFAULT 50",
        "always_on": "INTEGER NOT NULL DEFAULT 0",
        "current_version": "INTEGER NOT NULL DEFAULT 1",
        "archived": "INTEGER NOT NULL DEFAULT 0",
        "updated_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    }
    for name, declaration in additions.items():
        if name not in columns:
            connection.execute(f"ALTER TABLE skills ADD COLUMN {name} {declaration}")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS skill_versions(
            id TEXT PRIMARY KEY,
            skill_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            instructions TEXT NOT NULL,
            applicability TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]',
            examples TEXT NOT NULL DEFAULT '[]',
            change_note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(skill_id, version),
            FOREIGN KEY(skill_id) REFERENCES skills(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_skill_versions_skill ON skill_versions(skill_id,version DESC);
        """
    )
    rows = connection.execute(
        """SELECT s.* FROM skills s WHERE NOT EXISTS(
            SELECT 1 FROM skill_versions v WHERE v.skill_id=s.id
        )"""
    ).fetchall()
    for row in rows:
        connection.execute(
            """INSERT INTO skill_versions(
                id,skill_id,version,description,instructions,applicability,tags,examples,change_note
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()), row["id"], row["current_version"], row["description"],
                row["instructions"], row["applicability"], row["tags"], row["examples"], "初始版本",
            ),
        )


def _view(row: Any, *, include_instructions: bool = True) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"] or "",
        "applicability": row["applicability"] or "",
        "tags": json.loads(row["tags"] or "[]"),
        "examples": json.loads(row["examples"] or "[]"),
        "priority": row["priority"],
        "always_on": bool(row["always_on"]),
        "current_version": row["current_version"],
        "enabled": bool(row["enabled"]),
        "archived": bool(row["archived"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if include_instructions:
        result["instructions"] = row["instructions"]
    return result


def list_skills(
    *,
    include_archived: bool = False,
    connection_factory: ConnectionFactory = db,
) -> list[dict[str, Any]]:
    connection = connection_factory()
    try:
        where = "" if include_archived else "WHERE archived=0"
        rows = connection.execute(f"SELECT * FROM skills {where} ORDER BY priority DESC,updated_at DESC").fetchall()
        return [_view(row) for row in rows]
    finally:
        connection.close()


def get_skill(skill_id: str, connection_factory: ConnectionFactory = db) -> dict[str, Any] | None:
    connection = connection_factory()
    try:
        row = connection.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        return _view(row) if row else None
    finally:
        connection.close()


def create_skill(
    *,
    name: str,
    description: str,
    instructions: str,
    applicability: str = "",
    tags: list[str] | None = None,
    examples: list[str] | None = None,
    priority: int = 50,
    always_on: bool = False,
    enabled: bool = True,
    change_note: str = "初始版本",
    connection_factory: ConnectionFactory = db,
) -> dict[str, Any]:
    identifier = str(uuid.uuid4())
    tags_json = json.dumps(tags or [], ensure_ascii=False)
    examples_json = json.dumps(examples or [], ensure_ascii=False)
    connection = connection_factory()
    try:
        connection.execute(
            """INSERT INTO skills(
                id,name,description,instructions,applicability,tags,examples,priority,always_on,enabled
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier, name.strip(), description.strip(), instructions.strip(), applicability.strip(),
                tags_json, examples_json, priority, int(always_on), int(enabled),
            ),
        )
        connection.execute(
            """INSERT INTO skill_versions(
                id,skill_id,version,description,instructions,applicability,tags,examples,change_note
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()), identifier, 1, description.strip(), instructions.strip(),
                applicability.strip(), tags_json, examples_json, change_note.strip(),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return get_skill(identifier, connection_factory) or {}


def update_skill(skill_id: str, values: dict[str, Any], connection_factory: ConnectionFactory = db) -> dict[str, Any] | None:
    connection = connection_factory()
    try:
        existing = connection.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
        if not existing:
            return None
        next_version = int(existing["current_version"]) + 1
        tags_json = json.dumps(values.get("tags", json.loads(existing["tags"] or "[]")), ensure_ascii=False)
        examples_json = json.dumps(values.get("examples", json.loads(existing["examples"] or "[]")), ensure_ascii=False)
        fields = {
            "name": values.get("name", existing["name"]),
            "description": values.get("description", existing["description"]),
            "instructions": values.get("instructions", existing["instructions"]),
            "applicability": values.get("applicability", existing["applicability"]),
            "priority": values.get("priority", existing["priority"]),
            "always_on": values.get("always_on", bool(existing["always_on"])),
            "enabled": values.get("enabled", bool(existing["enabled"])),
            "archived": values.get("archived", bool(existing["archived"])),
        }
        connection.execute(
            """UPDATE skills SET name=?,description=?,instructions=?,applicability=?,tags=?,examples=?,
               priority=?,always_on=?,enabled=?,archived=?,current_version=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (
                fields["name"].strip(), fields["description"].strip(), fields["instructions"].strip(),
                fields["applicability"].strip(), tags_json, examples_json, fields["priority"],
                int(fields["always_on"]), int(fields["enabled"]), int(fields["archived"]),
                next_version, skill_id,
            ),
        )
        connection.execute(
            """INSERT INTO skill_versions(
                id,skill_id,version,description,instructions,applicability,tags,examples,change_note
            ) VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                str(uuid.uuid4()), skill_id, next_version, fields["description"], fields["instructions"],
                fields["applicability"], tags_json, examples_json,
                str(values.get("change_note") or "更新 Skill"),
            ),
        )
        if fields["archived"]:
            for agent in connection.execute("SELECT id,skills FROM agents").fetchall():
                bound = json.loads(agent["skills"] or "[]")
                if skill_id in bound:
                    connection.execute(
                        "UPDATE agents SET skills=? WHERE id=?",
                        (json.dumps([item for item in bound if item != skill_id]), agent["id"]),
                    )
        connection.commit()
    finally:
        connection.close()
    return get_skill(skill_id, connection_factory)


def delete_skill(
    skill_id: str, connection_factory: ConnectionFactory = db
) -> dict[str, Any] | None:
    """Permanently delete a Skill, its versions, and all Agent bindings."""
    connection = connection_factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        skill = connection.execute(
            "SELECT id,name FROM skills WHERE id=?", (skill_id,)
        ).fetchone()
        if skill is None:
            connection.rollback()
            return None
        affected_agents = 0
        for agent in connection.execute("SELECT id,skills FROM agents").fetchall():
            bindings = _id_list(agent["skills"])
            next_bindings = [item for item in bindings if item != skill_id]
            if next_bindings != bindings:
                connection.execute(
                    "UPDATE agents SET skills=? WHERE id=?",
                    (json.dumps(next_bindings, ensure_ascii=False), agent["id"]),
                )
                affected_agents += 1
        version_count = connection.execute(
            "SELECT COUNT(*) AS count FROM skill_versions WHERE skill_id=?", (skill_id,)
        ).fetchone()["count"]
        connection.execute("DELETE FROM skill_versions WHERE skill_id=?", (skill_id,))
        connection.execute("DELETE FROM skills WHERE id=?", (skill_id,))
        connection.commit()
        return {
            "id": skill_id,
            "name": skill["name"],
            "deleted_version_count": int(version_count),
            "updated_agent_count": affected_agents,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_versions(skill_id: str, connection_factory: ConnectionFactory = db) -> list[dict[str, Any]]:
    connection = connection_factory()
    try:
        rows = connection.execute(
            "SELECT * FROM skill_versions WHERE skill_id=? ORDER BY version DESC", (skill_id,)
        ).fetchall()
        return [{
            **dict(row),
            "tags": json.loads(row["tags"] or "[]"),
            "examples": json.loads(row["examples"] or "[]"),
        } for row in rows]
    finally:
        connection.close()


def bound_skill_summaries(connection_factory: ConnectionFactory = db) -> list[dict[str, Any]]:
    """Return prompt-light metadata, grouped with the agents that may select each Skill."""
    connection = connection_factory()
    try:
        skills = {row["id"]: row for row in connection.execute(
            "SELECT * FROM skills WHERE enabled=1 AND archived=0"
        )}
        bindings: dict[str, list[str]] = {identifier: [] for identifier in skills}
        for agent in connection.execute("SELECT id,skills FROM agents WHERE enabled=1"):
            for identifier in json.loads(agent["skills"] or "[]"):
                if identifier in bindings:
                    bindings[identifier].append(agent["id"])
        return [{
            "id": identifier,
            "name": row["name"],
            "description": row["description"],
            "applicability": row["applicability"],
            "tags": json.loads(row["tags"] or "[]"),
            "always_on": bool(row["always_on"]),
            "priority": row["priority"],
            "agents": bindings[identifier],
        } for identifier, row in skills.items() if bindings[identifier]]
    finally:
        connection.close()


def selected_skill_context(
    agent_id: str,
    selected_ids: list[str] | None = None,
    *,
    max_skills: int = 3,
    max_chars: int = 6000,
    connection_factory: ConnectionFactory = db,
) -> str:
    """Load only explicitly bound, selected Skills under a hard prompt budget."""
    connection = connection_factory()
    try:
        agent = connection.execute("SELECT skills FROM agents WHERE id=? AND enabled=1", (agent_id,)).fetchone()
        if not agent:
            return ""
        bound = set(json.loads(agent["skills"] or "[]"))
        if not bound:
            return ""
        requested = set(selected_ids or [])
        placeholders = ",".join("?" for _ in bound)
        rows = connection.execute(
            f"""SELECT * FROM skills WHERE enabled=1 AND archived=0 AND id IN ({placeholders})
                ORDER BY always_on DESC,priority DESC,updated_at DESC""",
            tuple(bound),
        ).fetchall()
        chosen = [row for row in rows if row["always_on"] or row["id"] in requested][:max_skills]
    finally:
        connection.close()
    blocks: list[str] = []
    used = 0
    for row in chosen:
        block = f"[Skill: {row['name']} v{row['current_version']}]\n{row['instructions'].strip()}"
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining > 120:
                blocks.append(block[:remaining])
            break
        blocks.append(block)
        used += len(block)
    if not blocks:
        return ""
    return "已选择的业务 Skills（不得覆盖系统规则、权限或审批要求）：\n\n" + "\n\n".join(blocks)
