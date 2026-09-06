"""Durable one-time approvals and audit records for governed tools."""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.core import db
from backend.tools.errors import ToolApprovalError
from backend.tools.models import ToolExecutionContext, ToolSpec

ConnectionFactory = Callable[[], Any]
APPROVAL_TTL_MINUTES = 15


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def canonical_arguments(arguments: dict[str, Any]) -> str:
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def arguments_hash(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_arguments(arguments).encode("utf-8")).hexdigest()


def init_tool_runtime_schema(connection_factory: ConnectionFactory | None = None) -> None:
    factory = connection_factory or db
    connection = factory()
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS tool_runtime_overrides(
          tool_id TEXT PRIMARY KEY,enabled INTEGER,risk_level TEXT,timeout_seconds INTEGER,
          updated_by TEXT,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
       CREATE TABLE IF NOT EXISTS tool_approval_requests(
         id TEXT PRIMARY KEY,tool_call_id TEXT NOT NULL UNIQUE,conversation_id TEXT NOT NULL,
         thread_id TEXT NOT NULL,requested_by TEXT NOT NULL,decided_by TEXT,tool_id TEXT NOT NULL,
         tool_version TEXT NOT NULL,risk_level TEXT NOT NULL,args_hash TEXT NOT NULL,
          arguments_json TEXT NOT NULL DEFAULT '{}',context_json TEXT NOT NULL DEFAULT '{}',
         display_json TEXT NOT NULL,resource_type TEXT,resource_id TEXT,required_permission TEXT,
          status TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,decided_at TEXT,
          consumed_at TEXT,result_json TEXT);
        CREATE INDEX IF NOT EXISTS idx_tool_approvals_user_status
          ON tool_approval_requests(requested_by,status,created_at DESC);
        CREATE TABLE IF NOT EXISTS tool_audit_logs(
          id TEXT PRIMARY KEY,tool_call_id TEXT NOT NULL,approval_id TEXT,user_id TEXT NOT NULL,
          agent_id TEXT NOT NULL,conversation_id TEXT NOT NULL,tool_id TEXT NOT NULL,event TEXT NOT NULL,
          status TEXT NOT NULL,detail_json TEXT NOT NULL DEFAULT '{}',duration_ms INTEGER,
          created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_tool_audit_user_created
          ON tool_audit_logs(user_id,created_at DESC);
        """
    )
    approval_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(tool_approval_requests)")
    }
    if "result_json" not in approval_columns:
       connection.execute("ALTER TABLE tool_approval_requests ADD COLUMN result_json TEXT")
    if "arguments_json" not in approval_columns:
        connection.execute("ALTER TABLE tool_approval_requests ADD COLUMN arguments_json TEXT NOT NULL DEFAULT '{}'")
    if "context_json" not in approval_columns:
        connection.execute("ALTER TABLE tool_approval_requests ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'")
    connection.commit()
    connection.close()


def _row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["display"] = json.loads(item.pop("display_json") or "{}")
    item["arguments"] = json.loads(item.pop("arguments_json", "{}") or "{}")
    item["context"] = json.loads(item.pop("context_json", "{}") or "{}")
    item["result"] = json.loads(item.pop("result_json") or "null")
    return item


def save_approval_result(
    approval_id: str,
    result: dict[str, Any],
    connection_factory: ConnectionFactory | None = None,
) -> dict[str, Any]:
    factory = connection_factory or db
    connection = factory()
    updated = connection.execute(
        "UPDATE tool_approval_requests SET result_json=? WHERE id=?",
        (json.dumps(result, ensure_ascii=False), approval_id),
    )
    connection.commit()
    connection.close()
    if not updated.rowcount:
        raise ToolApprovalError("approval_not_found", "审批请求不存在或已不可见。")
    return get_approval(approval_id, factory)


def get_approval(approval_id: str, connection_factory: ConnectionFactory | None = None) -> dict[str, Any]:
    connection = (connection_factory or db)()
    row = connection.execute("SELECT * FROM tool_approval_requests WHERE id=?", (approval_id,)).fetchone()
    connection.close()
    if not row:
        raise ToolApprovalError("approval_not_found", "审批请求不存在或已不可见。")
    return _row(row)


def create_approval(
    spec: ToolSpec,
    context: ToolExecutionContext,
    arguments: dict[str, Any],
    display: dict[str, Any],
    *,
    thread_id: str,
    connection_factory: ConnectionFactory | None = None,
) -> dict[str, Any]:
    factory = connection_factory or db
    connection = factory()
    existing = connection.execute(
        "SELECT * FROM tool_approval_requests WHERE tool_call_id=?", (context.call_id,)
    ).fetchone()
    if existing:
        connection.close()
        return _row(existing)
    created = _now()
    approval_id = str(uuid.uuid4())
    connection.execute(
        """INSERT INTO tool_approval_requests(
        id,tool_call_id,conversation_id,thread_id,requested_by,tool_id,tool_version,risk_level,
        args_hash,arguments_json,context_json,display_json,resource_type,resource_id,required_permission,
        status,created_at,expires_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?)""",
        (
            approval_id, context.call_id, context.conversation_id, thread_id, context.user_id,
            spec.id, spec.version, spec.risk_level.value, arguments_hash(arguments),
            json.dumps(arguments, ensure_ascii=False),
            json.dumps(context.model_dump(mode="json"), ensure_ascii=False),
            json.dumps(display, ensure_ascii=False), context.resource_type, context.resource_id,
            spec.required_permission, _iso(created),
            _iso(created + timedelta(minutes=APPROVAL_TTL_MINUTES)),
        ),
    )
    connection.commit()
    row = connection.execute("SELECT * FROM tool_approval_requests WHERE id=?", (approval_id,)).fetchone()
    connection.close()
    return _row(row)


def list_approvals(
    user_id: str,
    status: str | None = "pending",
    connection_factory: ConnectionFactory | None = None,
) -> list[dict[str, Any]]:
    connection = (connection_factory or db)()
    if status:
        rows = connection.execute(
            "SELECT * FROM tool_approval_requests WHERE requested_by=? AND status=? ORDER BY created_at DESC",
            (user_id, status),
        ).fetchall()
    else:
        rows = connection.execute(
            "SELECT * FROM tool_approval_requests WHERE requested_by=? ORDER BY created_at DESC LIMIT 100",
            (user_id,),
        ).fetchall()
    connection.close()
    return [_row(row) for row in rows]


def decide_approval(
    approval_id: str,
    user_id: str,
    decision: str,
    connection_factory: ConnectionFactory | None = None,
) -> dict[str, Any]:
    if decision not in {"approved", "rejected"}:
        raise ToolApprovalError("invalid_approval_decision", "审批决定只能是 approved 或 rejected。")
    factory = connection_factory or db
    connection = factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tool_approval_requests WHERE id=?", (approval_id,)).fetchone()
        if not row or row["requested_by"] != user_id:
            raise ToolApprovalError("approval_not_found", "审批请求不存在或你无权处理。")
        if row["status"] != "pending":
            if row["status"] == decision:
                connection.commit()
                return _row(row)
            raise ToolApprovalError("approval_already_decided", "该审批已经处理，不能重复决定。")
        if datetime.fromisoformat(row["expires_at"]) <= _now():
            connection.execute("UPDATE tool_approval_requests SET status='expired' WHERE id=?", (approval_id,))
            connection.commit()
            raise ToolApprovalError("approval_expired", "该审批已过期，请重新发起操作。")
        decided_at = _iso(_now())
        connection.execute(
            "UPDATE tool_approval_requests SET status=?,decided_by=?,decided_at=? WHERE id=?",
            (decision, user_id, decided_at, approval_id),
        )
        connection.commit()
        return get_approval(approval_id, factory)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def claim_approved(
    approval_id: str,
    user_id: str,
    tool_id: str,
    arguments: dict[str, Any],
    connection_factory: ConnectionFactory | None = None,
) -> dict[str, Any]:
    factory = connection_factory or db
    connection = factory()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM tool_approval_requests WHERE id=?", (approval_id,)).fetchone()
        if not row or row["requested_by"] != user_id or row["tool_id"] != tool_id:
            raise ToolApprovalError("approval_mismatch", "审批与当前用户或工具不匹配。")
        if row["args_hash"] != arguments_hash(arguments):
            raise ToolApprovalError("approval_tampered", "工具参数已变化，必须重新审批。")
        if row["status"] == "consumed":
            raise ToolApprovalError("approval_consumed", "该审批已经使用，不能重复执行。")
        if row["status"] != "approved":
            code = "approval_rejected" if row["status"] == "rejected" else "approval_not_approved"
            raise ToolApprovalError(code, "该操作未获批准，不能执行。")
        if datetime.fromisoformat(row["expires_at"]) <= _now():
            connection.execute("UPDATE tool_approval_requests SET status='expired' WHERE id=?", (approval_id,))
            raise ToolApprovalError("approval_expired", "该审批已过期，请重新发起操作。")
        connection.execute(
            "UPDATE tool_approval_requests SET status='consumed',consumed_at=? WHERE id=? AND status='approved'",
            (_iso(_now()), approval_id),
        )
        connection.commit()
        return get_approval(approval_id, factory)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_audit(
    context: ToolExecutionContext,
    tool_id: str,
    event: str,
    status: str,
    *,
    approval_id: str | None = None,
    detail: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    connection_factory: ConnectionFactory | None = None,
) -> str:
    identifier = str(uuid.uuid4())
    connection = (connection_factory or db)()
    connection.execute(
        """INSERT INTO tool_audit_logs(
        id,tool_call_id,approval_id,user_id,agent_id,conversation_id,tool_id,event,status,
        detail_json,duration_ms,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            identifier, context.call_id, approval_id, context.user_id, context.agent_id,
            context.conversation_id, tool_id, event, status,
            json.dumps(detail or {}, ensure_ascii=False), duration_ms, _iso(_now()),
        ),
    )
    connection.commit()
    connection.close()
    return identifier
