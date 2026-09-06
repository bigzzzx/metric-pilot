"""In-process registry that is the single source of truth for tools."""
from __future__ import annotations

from threading import RLock

from backend.tools.errors import ToolNotFound
from backend.tools.models import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._items: dict[str, ToolSpec] = {}
        self._lock = RLock()

    def register(self, spec: ToolSpec) -> ToolSpec:
        with self._lock:
            if spec.id in self._items:
                raise ValueError(f"Tool is already registered: {spec.id}")
            self._items[spec.id] = spec
        return spec

    def replace(self, spec: ToolSpec) -> ToolSpec:
        """Atomically add or refresh a runtime-managed tool."""
        with self._lock:
            self._items[spec.id] = spec
        return spec

    def remove(self, tool_id: str) -> None:
        with self._lock:
            self._items.pop(tool_id, None)

    def get(self, tool_id: str) -> ToolSpec:
        try:
            return self._items[tool_id]
        except KeyError as exc:
            raise ToolNotFound(tool_id) from exc

    def all(self) -> tuple[ToolSpec, ...]:
        return tuple(self._items[key] for key in sorted(self._items))

    def catalog(self) -> list[dict]:
        return [spec.catalog_item() for spec in self.all()]

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


registry = ToolRegistry()


def initialized_registry() -> ToolRegistry:
    """Explicitly load built-ins before a catalog or management consumer reads them."""

    from backend.tools.builtins import register_builtin_tools

    register_builtin_tools(registry)
    try:
        from backend.mcp.runtime import register_mcp_tools
        register_mcp_tools(registry)
    except Exception:
        # Storage migrations and optional MCP dependencies may not be ready yet.
        pass
    return registry
