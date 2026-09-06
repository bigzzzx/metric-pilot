"""Platform-admin user, role, group and audit management."""
from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException

from backend.catalog.service import is_platform_admin
from backend.core import db


def require_platform_admin(user_id: str) -> None:
    if not is_platform_admin(user_id):
        raise HTTPException(403, "Platform administrator permission is required")


def _items(connection, sql: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, values)]


def administration_overview(actor_id: str) -> dict[str, Any]:
    require_platform_admin(actor_id)
    con = db()
    try:
        roles = _items(con, "SELECT id,name,display_name,description FROM catalog_roles ORDER BY display_name")
        for role in roles:
            role["policies"] = _items(con, "SELECT resource_type,resource_id,action,effect FROM catalog_policies WHERE subject_type='role' AND subject_id=? ORDER BY resource_type,action", (role["id"],))

        groups = _items(con, "SELECT id,name,display_name,description FROM catalog_groups ORDER BY display_name")
        for group in groups:
            group["member_count"] = con.execute("SELECT COUNT(*) AS count FROM catalog_group_memberships WHERE group_id=?", (group["id"],)).fetchone()["count"]
            group["domain_ids"] = [row["resource_id"] for row in con.execute("SELECT DISTINCT resource_id FROM catalog_policies WHERE subject_type='group' AND subject_id=? AND resource_type='domain' AND effect='allow' ORDER BY resource_id", (group["id"],))]

        users = _items(con, "SELECT id,username,is_active,created_at FROM users ORDER BY username")
        for user in users:
            user["role_ids"] = [row["role_id"] for row in con.execute("SELECT role_id FROM catalog_user_roles WHERE user_id=? ORDER BY role_id", (user["id"],))]
            user["group_ids"] = [row["group_id"] for row in con.execute("SELECT group_id FROM catalog_group_memberships WHERE user_id=? ORDER BY group_id", (user["id"],))]

        domains = _items(con, "SELECT id,name,display_name,description FROM catalog_domains WHERE active=1 ORDER BY display_name")
        return {"users": users, "roles": roles, "groups": groups, "domains": domains}
    finally:
        con.close()


def update_user_access(actor_id: str, user_id: str, *, role_ids: list[str], group_ids: list[str], is_active: bool) -> dict[str, Any]:
    """Update grants atomically while preserving a usable platform administrator."""
    require_platform_admin(actor_id)
    con = db()
    try:
        target = con.execute("SELECT id,username,is_active FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(404, "User not found")
        valid_roles = {row["id"] for row in con.execute("SELECT id FROM catalog_roles")}
        valid_groups = {row["id"] for row in con.execute("SELECT id FROM catalog_groups")}
        desired_roles, desired_groups = set(role_ids), set(group_ids)
        if not desired_roles:
            raise HTTPException(422, "Every user must have at least one platform role")
        if unknown := desired_roles - valid_roles:
            raise HTTPException(422, f"Unknown role: {sorted(unknown)[0]}")
        if unknown := desired_groups - valid_groups:
            raise HTTPException(422, f"Unknown group: {sorted(unknown)[0]}")

        platform_role = "role-platform-admin"
        currently_admin = bool(con.execute("SELECT 1 FROM catalog_user_roles WHERE user_id=? AND role_id=?", (user_id, platform_role)).fetchone())
        will_be_admin = platform_role in desired_roles
        if actor_id == user_id and (not will_be_admin or not is_active):
            raise HTTPException(422, "You cannot remove or disable your own platform administrator access")
        if currently_admin and (not will_be_admin or not is_active):
            active_admins = con.execute("""
                SELECT COUNT(DISTINCT r.user_id) AS count FROM catalog_user_roles r
                JOIN users u ON u.id=r.user_id
                WHERE r.role_id=? AND u.is_active=1
            """, (platform_role,)).fetchone()["count"]
            if active_admins <= 1:
                raise HTTPException(422, "At least one active platform administrator must remain")

        before = {
            "role_ids": [row["role_id"] for row in con.execute("SELECT role_id FROM catalog_user_roles WHERE user_id=?", (user_id,))],
            "group_ids": [row["group_id"] for row in con.execute("SELECT group_id FROM catalog_group_memberships WHERE user_id=?", (user_id,))],
            "is_active": bool(target["is_active"]),
        }
        con.execute("DELETE FROM catalog_user_roles WHERE user_id=?", (user_id,))
        con.executemany("INSERT INTO catalog_user_roles(role_id,user_id) VALUES (?,?)", [(role_id, user_id) for role_id in sorted(desired_roles)])
        con.execute("DELETE FROM catalog_group_memberships WHERE user_id=?", (user_id,))
        con.executemany("INSERT INTO catalog_group_memberships(group_id,user_id) VALUES (?,?)", [(group_id, user_id) for group_id in sorted(desired_groups)])
        con.execute("UPDATE users SET is_active=? WHERE id=?", (int(is_active), user_id))
        after = {"role_ids": sorted(desired_roles), "group_ids": sorted(desired_groups), "is_active": bool(is_active)}
        con.execute("INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json) VALUES (?,?,?,?,?)", (actor_id, "user_access_updated", "user", user_id, json.dumps({"before": before, "after": after}, ensure_ascii=False)))
        con.commit()
        return {"id": target["id"], "username": target["username"], **after}
    finally:
        con.close()


def audit_logs(actor_id: str, limit: int = 100) -> list[dict[str, Any]]:
    require_platform_admin(actor_id)
    con = db()
    try:
        rows = _items(con, """
            SELECT log.id,log.action,log.entity_type,log.entity_id,log.detail_json,log.created_at,
                   actor.username AS actor_username
            FROM catalog_audit_logs log LEFT JOIN users actor ON actor.id=log.actor_id
            ORDER BY log.id DESC LIMIT ?
        """, (limit,))
        for row in rows:
            try: row["detail"] = json.loads(row.pop("detail_json") or "{}")
            except json.JSONDecodeError: row["detail"] = {}
        return rows
    finally:
        con.close()
