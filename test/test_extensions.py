import json
import sqlite3

from backend import main as main_module
import backend.extensions as extensions_module
from backend.skills import repository as skill_repository
from backend.main import init_storage


def test_builtin_tool_catalog_has_guarded_query_tool():
    assert any(tool["id"] == "readonly_sql" for tool in extensions_module.tools())


def test_skill_is_injected_only_after_explicit_agent_binding(tmp_path, monkeypatch):
    def factory():
        connection = sqlite3.connect(tmp_path / "extensions.db")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    monkeypatch.setattr(main_module, "db", factory)
    monkeypatch.setattr(skill_repository, "db", factory)
    monkeypatch.setattr(
        extensions_module, "create_versioned_skill",
        lambda **values: skill_repository.create_skill(connection_factory=factory, **values),
    )
    monkeypatch.setattr(
        extensions_module, "selected_skill_context",
        lambda agent_id, selected_ids=None: skill_repository.selected_skill_context(
            agent_id, selected_ids, connection_factory=factory,
        ),
    )
    init_storage()
    connection = factory()
    connection.execute(
        "INSERT OR IGNORE INTO agents(id,name,description,model_id,tools,system_prompt,enabled,mcp_servers,mcp_tools,skills) VALUES (?,?,?,?,?,?,1,'[]','[]','[]')",
        ("data-query-agent", "Data Query", "", "m1", "[]", ""),
    )
    connection.commit()
    connection.close()
    skill = extensions_module.create_skill("pytest-skill", "test", "Always state the data scope before giving a metric conclusion.")

    assert extensions_module.skill_context("data-query-agent", [skill["id"]]) == ""
    connection = factory()
    connection.execute(
        "UPDATE agents SET skills=? WHERE id='data-query-agent'",
        (json.dumps([skill["id"]]),),
    )
    connection.commit()
    connection.close()

    context = extensions_module.skill_context("data-query-agent", [skill["id"]])

    assert "Always state the data scope" in context
    connection = factory()
    connection.execute("UPDATE agents SET skills='[]' WHERE id='data-query-agent'")
    connection.execute("DELETE FROM skill_versions WHERE skill_id=?", (skill["id"],))
    connection.execute("DELETE FROM skills WHERE id=?", (skill["id"],))
    connection.commit()
    connection.close()
