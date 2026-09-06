import pytest

from backend import memory
from backend.memory import extract_explicit_memory


def test_only_explicit_requests_become_long_term_memories():
    assert extract_explicit_memory("我今天在北京") is None
    assert extract_explicit_memory("你可以叫我 xuan") is None
    assert extract_explicit_memory("记住我叫 xuan") == ("profile", "用户希望被称为 xuan。")
    assert extract_explicit_memory("以后默认按 GMV 口径分析") == (
        "working_convention",
        "默认约定：GMV 口径分析",
    )


def test_explicit_past_event_becomes_episodic_memory():
    assert extract_explicit_memory("请记住上次排查连接失败是代理配置导致的") == (
        "episodic",
        "历史事件：上次排查连接失败是代理配置导致的",
    )


def test_explicit_generic_memory_is_bounded():
    kind, content = extract_explicit_memory("请记住我负责华东区域的数据分析")
    assert kind == "preference"
    assert "华东区域" in content


@pytest.mark.parametrize(
    "message",
    [
        "记得我喜欢直接说重点",
        "请记得我喜欢直接说重点",
        "记下我喜欢直接说重点",
        "记一下我喜欢直接说重点",
        "帮我记住我喜欢直接说重点",
        "请帮我记住我喜欢直接说重点",
    ],
)
def test_common_explicit_memory_commands_are_recognized(message):
    assert extract_explicit_memory(message) == (
        "preference",
        "用户明确要求记住：我喜欢直接说重点",
    )


@pytest.mark.parametrize(
    "message",
    [
        "你记得我喜欢什么吗？",
        "记得我喜欢什么吗",
        "你还记得我的昵称吗?",
        "记住了吗？",
    ],
)
def test_memory_questions_do_not_create_long_term_memory(message):
    assert extract_explicit_memory(message) is None


def test_new_command_forms_keep_profile_and_default_semantics():
    assert extract_explicit_memory("请记下叫我 xuan") == (
        "profile",
        "用户希望被称为 xuan。",
    )
    assert extract_explicit_memory("记得以后默认按 GMV 口径分析") == (
        "working_convention",
        "默认约定：GMV 口径分析",
    )
    assert extract_explicit_memory("请以后叫我 xuan") == (
        "profile",
        "用户希望被称为 xuan。",
    )
    assert extract_explicit_memory("请以后默认用中文回答") == (
        "working_convention",
        "默认约定：中文回答",
    )


def test_online_memory_save_forwards_source_message_id(monkeypatch):
    captured = {}

    def fake_upsert(user_id, memory_type, content, conversation_id, source_message_id):
        captured.update(
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            conversation_id=conversation_id,
            source_message_id=source_message_id,
        )
        return {"id": "memory-1", **captured}

    monkeypatch.setattr(memory, "upsert_user_memory", fake_upsert)

    saved = memory.remember_explicit_request(
        "记得我喜欢直接说重点",
        "user-a",
        "conversation-a",
        "message-a",
    )

    assert saved["id"] == "memory-1"
    assert captured["source_message_id"] == "message-a"
