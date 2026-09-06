# Conversation Context and Memory Design

## Goal

Keep conversations durable without repeatedly sending the entire transcript to
the model. Each request will receive a bounded, useful context made from a
conversation summary, recent raw turns, and only explicitly confirmed
user-level memories.

## Scope and terminology

All data is stored in the existing local SQLite platform database.

- **Transcript**: every completed user, tool, and assistant message. It stays
  available for UI recovery and audit, but is not all sent to the model.
- **Conversation summary**: a compressed account of older completed turns.
  It is scoped to one conversation and survives restarts.
- **Recent context**: the newest twelve successful raw messages in the active
  conversation.
- **Long-term memory**: a small, user-scoped fact or preference. It is written
  only when the user explicitly asks to retain it, for example `记住我叫 xuan`
  or `以后默认按 GMV 口径`.

Failed and pending turns must remain visible in the transcript but must never
be used as model context or summarization input.

## Storage

Two tables extend the existing `conversations` and `conversation_messages`
tables.

`conversation_summaries` has one current summary per conversation:

- `conversation_id` (primary key)
- `summary` (the structured natural-language summary)
- `covered_through_sequence_no` (the last message represented by the summary)
- timestamps

`user_memories` stores explicit cross-conversation memories:

- `id`, `user_id`, `memory_type`, `content`
- `source_conversation_id`, `source_message_id`
- timestamps and an `active` flag

The initial types are `preference`, `profile`, `metric_definition`, and
`working_convention`. Duplicate normalized content for the same user is
upserted rather than repeatedly stored.

## Context assembly

For each chat request, the backend constructs:

```text
Agent system prompt
+ active user long-term memories
+ active conversation summary
+ recent 12 successful raw messages
+ latest trusted tool result when data analysis requires it
+ current user message
```

This replaces the current direct `history[-10:]` slice. The LangGraph state
continues to be a `TypedDict`; it receives preassembled history plus explicit
`conversation_summary` and `user_memories` fields so nodes remain inspectable.

## Summary lifecycle

After a successful turn, the backend checks whether more than sixteen
successful raw messages exist after `covered_through_sequence_no`.

When the threshold is exceeded:

1. Select older completed turns, keeping the newest twelve raw messages out of
   the compaction range.
2. Ask the configured General Agent for a concise, factual Chinese summary.
3. Validate the response with Pydantic.
4. Atomically update the conversation summary and its covered sequence number.

If summary generation fails, the chat response remains successful and the
existing summary is retained. The next successful turn retries compaction.

## Explicit long-term memory lifecycle

The router first identifies clear memory requests using a Pydantic-validated
output. Only an affirmative request such as `记住`, `以后`, `默认`, or an
equivalent direct instruction is eligible. The memory extractor then produces
one or more concise facts, which are stored only after the request is accepted
by the extractor. Ordinary statements such as `我今天在北京` are not saved.

Future enhancement: expose a UI for listing, editing, and deleting memories.
This implementation will include backend retrieval and a minimal read-only
response payload, but not a full memory-management page.

## Error handling and privacy

- Data is always filtered by authenticated `user_id`; one user cannot retrieve
  another user's summary or long-term memories.
- Long-term memories are never inferred from failed requests.
- No raw API keys, provider responses, or SQL tool errors are stored as memory.
- Summary or extraction failures are logged with request IDs and do not fail
  the user-facing chat turn.

## Verification

Tests will cover:

1. Summary context plus recent-window assembly.
2. Threshold compaction and summary coverage updates.
3. Failed turns excluded from compaction and context.
4. Explicit memory persistence, duplicate upsert, and cross-user isolation.
5. Pydantic validation failures leave prior summaries/memories intact.
6. Existing conversation persistence and LangGraph tests remain green.
