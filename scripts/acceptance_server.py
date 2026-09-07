"""Run the actual app against a persistent, isolated acceptance workspace.

Reads only provider/model/agent configuration from the regular database.
Never copies users, sessions, knowledge, or business rows. Not a production launcher.
"""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    import backend.core as core

    source = core.PLATFORM_DB
    destination = (core.DATA_DIR / "acceptance-browser").resolve()
    assert destination.parent == core.DATA_DIR.resolve()
    destination.mkdir(exist_ok=True)
    core.DATA_DIR = destination
    core.PLATFORM_DB = destination / "platform.db"
    core.DEMO_DB = destination / "warehouse.db"
    first_start = not core.PLATFORM_DB.exists()
    core.db.__defaults__ = (core.PLATFORM_DB,)
    os.environ.update(EMBEDDING_BACKEND="hash", VECTOR_STORE_BACKEND="sqlite",
                      RERANK_BACKEND="none", MEMORY_VECTOR_BACKEND="disabled")
    os.environ.pop("DEEPSEEK_API_KEY", None)
    from backend.evaluation.fixtures import create_warehouse
    from backend.main import app, init_storage
    # Acceptance-only diagnostics: keep business text and credentials out of logs.
    import backend.model_client as model_client
    from backend.observability import log_event
    original_response_content = model_client._response_content

    def observed_response(response, provider_kind):
        try:
            return original_response_content(response, provider_kind)
        except model_client.ModelProviderError:
            try:
                payload = response.json()
                choice = (payload.get("choices") or [{}])[0]
                message = choice.get("message") or {}
                usage = payload.get("usage") or {}
                log_event("acceptance.provider_response_shape", http_status=response.status_code,
                          finish_reason=choice.get("finish_reason"),
                          content_chars=len(message.get("content") or ""),
                          reasoning_chars=len(message.get("reasoning_content") or ""),
                          refusal_present=bool(message.get("refusal")),
                          tool_calls=len(message.get("tool_calls") or []),
                          completion_tokens=usage.get("completion_tokens"))
            except (ValueError, TypeError, AttributeError):
                pass
            raise

    model_client._response_content = observed_response

    if not core.DEMO_DB.exists():
        create_warehouse(core.DEMO_DB)
    init_storage()
    # Keep secrets encrypted, and use the existing encryption key through core.secrets().
    if source.is_file() and first_start:
        original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        target = core.db()
        try:
            for table in ("providers", "models", "agents"):
                source_columns = {row[1] for row in original.execute(f'PRAGMA table_info("{table}")')}
                columns = [row[1] for row in target.execute(f'PRAGMA table_info("{table}")') if row[1] in source_columns]
                if not columns:
                    continue
                selected = ",".join('"' + name + '"' for name in columns)
                placeholders = ",".join("?" for _ in columns)
                target.executemany(f'INSERT OR REPLACE INTO "{table}" ({selected}) VALUES ({placeholders})',
                                   original.execute(f'SELECT {selected} FROM "{table}"').fetchall())
            target.commit()
        finally:
            original.close()
            target.close()
    init_storage()
    target = core.db()
    physical = sqlite3.connect(core.DEMO_DB)
    try:
        names = {row[0] for row in physical.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for asset in target.execute("SELECT id,physical_name FROM catalog_assets").fetchall():
            if asset["physical_name"] not in names:
                target.execute("UPDATE catalog_assets SET publish_status='draft' WHERE id=?", (asset["id"],))
                continue
            name = asset["physical_name"].replace('"', '""')
            fields = {row[1] for row in physical.execute(f'PRAGMA table_info("{name}")')}
            for column in target.execute("SELECT id,physical_name FROM catalog_columns WHERE asset_id=?", (asset["id"],)).fetchall():
                if column["physical_name"] not in fields:
                    target.execute("DELETE FROM catalog_columns WHERE id=?", (column["id"],))
        target.commit()
    finally:
        physical.close()
        target.close()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=3301)


if __name__ == "__main__":
    main()
