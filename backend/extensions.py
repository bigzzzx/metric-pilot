"""Compatibility facade for runtime-managed Agent extensions."""
from backend.mcp.repository import create_server, list_servers
from backend.skills.repository import create_skill as create_versioned_skill
from backend.skills.repository import list_skills as list_versioned_skills
from backend.skills.repository import selected_skill_context
from backend.tools.registry import initialized_registry


def tools() -> list[dict]:
    return initialized_registry().catalog()


def list_skills() -> list[dict]:
    return list_versioned_skills()


def create_skill(name: str, description: str, instructions: str, enabled: bool = True) -> dict:
    return create_versioned_skill(
        name=name, description=description, instructions=instructions, enabled=enabled,
    )


def skill_context(agent_id: str, selected_ids: list[str] | None = None) -> str:
    return selected_skill_context(agent_id, selected_ids)


def list_mcp_servers() -> list[dict]:
    return list_servers()


def create_mcp_server(name: str, transport: str, endpoint: str, api_key: str, enabled: bool) -> dict:
    return create_server(
        name=name, description="", transport=transport, endpoint=endpoint,
        auth_type="bearer", api_key=api_key, timeout_seconds=30, enabled=enabled,
    )
