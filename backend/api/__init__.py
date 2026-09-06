"""HTTP router package.

Domain services live outside this package; modules here only translate HTTP
requests, authentication and stable response contracts.
"""

from backend.api.admin import router as admin_router
from backend.api.catalog import router as catalog_router
from backend.api.knowledge import router as knowledge_router
from backend.api.tools import router as tools_router

__all__ = ["admin_router", "catalog_router", "knowledge_router", "tools_router"]
