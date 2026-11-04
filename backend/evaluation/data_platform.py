"""Isolated service acceptance environment; no live platform or warehouse mutation."""
from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver
from backend.evaluation.environment import evaluation_environment


@contextmanager
def data_platform_environment(directory):
    from backend import auth
    from backend.api import data_services
    from backend.catalog import publications
    from backend.data_services import metrics, production, production_runner, production_sql
    from backend.tools import approvals, approval_graph
    from backend.tools.approval_graph import build_approval_graph
    from backend.tools.builtins import register_builtin_tools
    from backend.tools.executor import ToolExecutor
    from backend.tools.registry import ToolRegistry
    with evaluation_environment(directory) as factory, ExitStack() as stack:
        for module in (metrics, production, production_runner, publications, data_services, auth, approvals):
            stack.enter_context(patch.object(module, 'db', factory))
        stack.enter_context(patch.object(production_sql, 'DEMO_DB', directory / 'warehouse.db'))
        con = factory()
        con.execute("INSERT INTO catalog_user_roles(role_id,user_id) VALUES ('role-platform-admin','eval-admin')")
        con.execute("INSERT INTO users(id,username,password_hash,is_active) VALUES ('eval-admin','platform-admin','not-a-login',1)")
        con.commit(); con.close()
        registry = ToolRegistry()
        register_builtin_tools(registry)
        runtime = ToolExecutor(registry, factory)
        stack.enter_context(patch.object(approval_graph, 'approval_graph', build_approval_graph(runtime, InMemorySaver())))
        yield factory, runtime, directory / 'warehouse.db'
