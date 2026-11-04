"""Central policy evaluation for every registered tool invocation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from backend.core import db
from backend.tools.models import RiskLevel, ToolExecutionContext, ToolSpec

ConnectionFactory = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    risk_level: RiskLevel
    code: str = "allowed"
    message: str = ""
    required_permission: str | None = None

    @property
    def approval_required(self) -> bool:
        return self.allowed and self.risk_level is RiskLevel.HIGH


def _subjects(connection: Any, user_id: str) -> list[tuple[str, str]]:
    groups = [("group", row["group_id"]) for row in connection.execute(
        "SELECT group_id FROM catalog_group_memberships WHERE user_id=?", (user_id,)
    )]
    roles = [("role", row["role_id"]) for row in connection.execute(
        "SELECT role_id FROM catalog_user_roles WHERE user_id=?", (user_id,)
    )]
    return [("user", user_id), *groups, *roles]


def _has_permission(
    connection: Any,
    user_id: str,
    permission: str,
    context: ToolExecutionContext,
) -> bool:
    if not permission:
        return True
    if connection.execute(
        "SELECT 1 FROM catalog_user_roles WHERE user_id=? AND role_id='role-platform-admin'",
        (user_id,),
    ).fetchone():
        return True
    policies: list[Any] = []
    for subject_type, subject_id in _subjects(connection, user_id):
        scopes = [("platform", "*")]
        if context.domain_id:
            scopes.append(("domain", context.domain_id))
        if context.resource_type and context.resource_id:
            scopes.append((context.resource_type, context.resource_id))
        for resource_type, resource_id in scopes:
            policies.extend(connection.execute(
                """SELECT effect FROM catalog_policies
                WHERE subject_type=? AND subject_id=? AND resource_type=?
                  AND resource_id IN (?, '*') AND action IN (?, '*')""",
                (subject_type, subject_id, resource_type, resource_id, permission),
            ).fetchall())
    if any(row["effect"] == "deny" for row in policies):
        return False
    return any(row["effect"] == "allow" for row in policies)


def evaluate_tool_policy(
    spec: ToolSpec,
    context: ToolExecutionContext,
    connection_factory: ConnectionFactory | None = None,
) -> PolicyDecision:
    connection = (connection_factory or db)()
    try:
        override = connection.execute(
            "SELECT enabled,risk_level FROM tool_runtime_overrides WHERE tool_id=?", (spec.id,)
        ).fetchone()
        enabled = spec.enabled if not override or override["enabled"] is None else bool(override["enabled"])
        risk = spec.risk_level
        if override and override["risk_level"]:
            risk = RiskLevel(override["risk_level"])
        if not enabled:
            return PolicyDecision(False, risk, "tool_disabled", f"工具“{spec.name}”当前未启用。")
        if spec.allowed_agents and context.agent_id not in spec.allowed_agents:
            return PolicyDecision(
                False, risk, "agent_tool_denied",
                f"当前智能体没有调用工具“{spec.name}”的权限。",
            )
        agent = connection.execute(
            "SELECT tools,mcp_servers,mcp_tools FROM agents WHERE id=? AND enabled=1", (context.agent_id,)
        ).fetchone()
        configured = json.loads(agent["tools"] or "[]") if agent else []
        if agent and spec.source.startswith("mcp:"):
            server_id = spec.source.removeprefix("mcp:")
            configured = [
                *json.loads(agent["mcp_tools"] or "[]"),
                *(spec.id for value in json.loads(agent["mcp_servers"] or "[]") if value == server_id),
            ]
        # This server-owned service has exactly one registered tool and no LLM.
        # It still passes the same user permission, risk and approval boundary.
        service_call = context.agent_id == 'production-service' and spec.id == 'etl_execute' and spec.source == 'builtin'
        if not service_call and (not agent or spec.id not in configured):
            return PolicyDecision(
                False, risk, "agent_tool_denied",
                f"当前智能体未绑定工具“{spec.name}”。",
            )
        if not _has_permission(connection, context.user_id, spec.required_permission, context):
            scope = context.resource_id or context.domain_id or "当前平台范围"
            message = (
                f"你没有执行“{spec.name}”所需的 {spec.required_permission} 权限"
                f"（范围：{scope}），请联系管理员申请对应权限。"
            )
            return PolicyDecision(
                False, risk, "permission_denied", message, spec.required_permission
            )
        return PolicyDecision(True, risk)
    finally:
        connection.close()
