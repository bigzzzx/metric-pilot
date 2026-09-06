"""Durable context compaction and explicit user-memory extraction."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from backend.conversations import compaction_candidate, save_conversation_summary, upsert_user_memory
from backend.model_client import ModelOutputError, ModelProviderError, complete_structured
from backend.observability import log_event
from backend.schemas import ConversationSummaryOutput


def _render_messages(items: list[Mapping[str, object]]) -> str:
    lines: list[str] = []
    for item in items:
        role = str(item["role"])
        if role == "tool":
            content = "查询工具结果：" + json.dumps(item.get("payload", {}).get("result", item.get("content", "")), ensure_ascii=False)
        else:
            content = str(item.get("content") or "")
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


async def compact_conversation_if_needed(conversation_id: str, user_id: str) -> bool:
    """Summarize old completed turns without making the chat response fail."""

    candidate = compaction_candidate(conversation_id, user_id)
    if not candidate:
        return False
    prompt = """你负责维护一个对话摘要。将已有摘要与新增较早对话压缩成事实准确的中文摘要。
保留：用户身份/偏好、已确认定义、数据分析结论和限制、未完成事项。
不要编造事实；不要把工具错误写成事实；不要输出 Markdown。"""
    source = (
        "已有摘要：\n" + (candidate["previous_summary"] or "（无）")
        + "\n\n新增对话：\n" + _render_messages(candidate["messages"])
    )
    try:
        output = await complete_structured(
            "general-agent",
            [{"role": "system", "content": prompt}, {"role": "user", "content": source}],
            ConversationSummaryOutput,
        )
        save_conversation_summary(
            conversation_id,
            user_id,
            output.summary,
            int(candidate["covered_through_sequence_no"]),
        )
        log_event("memory.conversation_compacted", conversation_id=conversation_id, covered_through_sequence_no=candidate["covered_through_sequence_no"])
        return True
    except (ModelProviderError, ModelOutputError, ValueError) as exc:
        log_event("memory.compaction_failed", level=30, conversation_id=conversation_id, error_type=type(exc).__name__)
        return False


_EXPLICIT_MEMORY_COMMAND = r"(?:请\s*)?(?:帮我\s*)?(?:记住|记得|记下|记一下)"
_QUESTION_ENDING = re.compile(r"(?:[?？]|(?:吗|么|嘛|呢)[。！!\s]*)$")


def _looks_like_memory_question(text: str) -> bool:
    """Reject questions about existing memory rather than save commands."""

    if re.match(r"^你(?:还)?记得", text):
        return True
    return bool(_QUESTION_ENDING.search(text))


def extract_explicit_memory(message: str) -> tuple[str, str] | None:
    """Extract only direct, declarative requests to persist cross-chat memory."""

    text = " ".join(message.strip().split())
    if not text or _looks_like_memory_question(text):
        return None

    nickname = re.search(
        rf"^(?:{_EXPLICIT_MEMORY_COMMAND}[，,:：]?\s*(?:以后\s*)?|(?:请\s*)?以后\s*)(?:我叫|叫我|称呼我)\s*(.{{1,80}})$",
        text,
        re.I,
    )
    if nickname:
        name = nickname.group(1).strip("。！!，, ")
        return "profile", f"用户希望被称为 {name}。"
    default = re.search(
        rf"^(?:(?:{_EXPLICIT_MEMORY_COMMAND})[，,:：]?\s*|请\s*)?(?:以后\s*)?默认(?:使用|采用|按|用)?\s*(.{{2,180}})$",
        text,
        re.I,
    )
    if default:
        return "working_convention", "默认约定：" + default.group(1).strip("。！! ")
    episode = re.search(
        rf"^{_EXPLICIT_MEMORY_COMMAND}[，,:：]?\s*((?:上次|此前|之前).{{2,180}})$", text, re.I
    )
    if episode:
        return "episodic", "历史事件：" + episode.group(1).strip("。！! ")
    explicit = re.search(
        rf"^{_EXPLICIT_MEMORY_COMMAND}[，,:：]?\s*(.{{2,180}})$", text, re.I
    )
    if explicit:
        return "preference", "用户明确要求记住：" + explicit.group(1).strip("。！! ")
    return None


def remember_explicit_request(
    message: str, user_id: str, conversation_id: str, source_message_id: str | None = None
) -> dict[str, object] | None:
    extracted = extract_explicit_memory(message)
    if not extracted:
        return None
    memory_type, content = extracted
    memory = upsert_user_memory(user_id, memory_type, content, conversation_id, source_message_id)
    log_event("memory.user_saved", user_id=user_id, conversation_id=conversation_id, memory_type=memory_type)
    return memory


MemorySaveCallback = Callable[
    [str, str, str, str, str | None], Mapping[str, Any]
]


def backfill_explicit_memories(
    records: Iterable[Mapping[str, object]],
    save_memory: MemorySaveCallback = upsert_user_memory,
) -> dict[str, int]:
    """Extract explicit memories from historical, successful user messages.

    ``records`` is deliberately storage-agnostic so a startup job or admin task
    can provide rows from any safe conversation query.  Each row must include
    ``user_id``, ``conversation_id`` and ``content``.  ``id`` or ``message_id``
    is forwarded as the source message id.  The default writer uses the
    normalized unique key in ``user_memories``, so running the same backfill
    repeatedly updates the existing memory rather than creating duplicates.

    Callers should normally pass only completed turns.  As a second guard,
    records explicitly marked ``successful=False``, ``completed=False`` or
    with a failed/pending ``status`` are ignored.
    """

    stats = {"scanned": 0, "saved": 0, "skipped": 0}
    for record in records:
        stats["scanned"] += 1
        role = str(record.get("role") or "user").lower()
        status = str(record.get("status") or "").lower()
        if (
            role != "user"
            or record.get("successful") is False
            or record.get("completed") is False
            or status in {"error", "failed", "pending"}
        ):
            stats["skipped"] += 1
            continue

        user_id = str(record.get("user_id") or "").strip()
        conversation_id = str(record.get("conversation_id") or "").strip()
        message = str(record.get("content") or "")
        extracted = extract_explicit_memory(message)
        if not user_id or not conversation_id or not extracted:
            stats["skipped"] += 1
            continue

        memory_type, content = extracted
        raw_message_id = (
            record.get("source_message_id")
            or record.get("message_id")
            or record.get("id")
        )
        source_message_id = str(raw_message_id) if raw_message_id else None
        save_memory(
            user_id,
            memory_type,
            content,
            conversation_id,
            source_message_id,
        )
        stats["saved"] += 1

    log_event("memory.backfill_completed", **stats)
    return stats
