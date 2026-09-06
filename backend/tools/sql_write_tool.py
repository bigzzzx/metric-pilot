"""Parameterized, transaction-safe writes for the local demo database only."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from backend.core import DEMO_DB
from backend.tools.errors import ToolRuntimeError
from backend.tools.models import SqlWriteDraft, SqlWriteInput, ToolExecutionContext

MAX_AFFECTED_ROWS = 100
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _quote(identifier: str) -> str:
    if not IDENTIFIER.fullmatch(identifier):
        raise ToolRuntimeError("invalid_write_target", f"非法表名或字段名：{identifier}")
    return f'"{identifier}"'


def _schema(connection: sqlite3.Connection, table: str) -> dict[str, dict[str, Any]]:
    _quote(table)
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? AND name NOT LIKE 'sqlite_%'",
        (table,),
    ).fetchone()
    if not exists:
        raise ToolRuntimeError("invalid_write_target", f"本地演示库不存在可写表“{table}”。")
    return {
        row[1]: {"type": row[2], "not_null": bool(row[3]), "primary_key": bool(row[5])}
        for row in connection.execute(f"PRAGMA table_info({_quote(table)})")
    }


def writable_schema_summary(database_path: Path = DEMO_DB) -> str:
    """Return the real local schema used to constrain the mutation planner."""

    connection = sqlite3.connect(database_path)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        lines = []
        for table in tables:
            columns = _schema(connection, table)
            rendered = ", ".join(
                f"{name} {details['type']}{' PRIMARY KEY' if details['primary_key'] else ''}"
                for name, details in columns.items()
            )
            lines.append(f"- {table}({rendered})")
        return "\n".join(lines)
    finally:
        connection.close()


def _validate_columns(columns: list[str], schema: dict[str, dict[str, Any]]) -> None:
    unknown = [column for column in columns if column not in schema]
    if unknown:
        raise ToolRuntimeError(
            "invalid_write_target",
            "写入计划包含不存在的字段：" + "、".join(unknown),
        )


def _where_clause(where) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    parameters: list[Any] = []
    seen: set[str] = set()
    for predicate in where:
        if predicate.column in seen:
            raise ToolRuntimeError("unsafe_write", f"WHERE 字段重复：{predicate.column}")
        seen.add(predicate.column)
        if predicate.value is None:
            clauses.append(f"{_quote(predicate.column)} IS NULL")
        else:
            clauses.append(f"{_quote(predicate.column)} = ?")
            parameters.append(predicate.value)
    return " AND ".join(clauses), parameters


def _count_matches(connection: sqlite3.Connection, payload: SqlWriteDraft) -> int:
    clause, parameters = _where_clause(payload.where)
    return int(connection.execute(
        f"SELECT COUNT(*) FROM {_quote(payload.table)} WHERE {clause}", parameters
    ).fetchone()[0])


def _validate_shape(payload: SqlWriteDraft, schema: dict[str, dict[str, Any]]) -> None:
    _validate_columns(list(payload.values), schema)
    _validate_columns([item.column for item in payload.where], schema)
    if payload.operation in {"update", "delete"} and not payload.where:
        raise ToolRuntimeError("unsafe_write", "UPDATE 和 DELETE 必须包含明确的 WHERE 条件。")
    if payload.operation in {"insert", "update"} and not payload.values:
        raise ToolRuntimeError("unsafe_write", f"{payload.operation.upper()} 必须包含待写入字段。")
    if payload.operation == "insert" and payload.where:
        raise ToolRuntimeError("unsafe_write", "INSERT 不能携带 WHERE 条件。")
    if payload.operation == "delete" and payload.values:
        raise ToolRuntimeError("unsafe_write", "DELETE 不能携带写入字段。")


def prepare_sql_write(
    plan: dict[str, Any] | SqlWriteDraft,
    database_path: Path = DEMO_DB,
) -> dict[str, Any]:
    """Validate a model draft and add a server-computed impact estimate."""

    try:
        payload = plan if isinstance(plan, SqlWriteDraft) else SqlWriteDraft.model_validate(plan)
    except Exception as exc:
        raise ToolRuntimeError("invalid_write_plan", f"写入计划格式不正确：{exc}") from exc
    connection = sqlite3.connect(database_path)
    try:
        schema = _schema(connection, payload.table)
        _validate_shape(payload, schema)
        estimated = 1 if payload.operation == "insert" else _count_matches(connection, payload)
        if estimated == 0:
            raise ToolRuntimeError("write_no_match", "WHERE 条件当前没有匹配任何记录，已取消写入。")
        if estimated > MAX_AFFECTED_ROWS:
            raise ToolRuntimeError(
                "write_scope_too_large",
                f"预计影响 {estimated} 行，超过单次允许的 {MAX_AFFECTED_ROWS} 行。",
            )
        return {**payload.model_dump(mode="json"), "estimated_rows": estimated}
    finally:
        connection.close()


def _execute_statement(connection: sqlite3.Connection, payload: SqlWriteInput) -> int:
    if payload.operation == "insert":
        columns = list(payload.values)
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO {_quote(payload.table)} "
            f"({', '.join(_quote(column) for column in columns)}) VALUES ({placeholders})"
        )
        parameters = [payload.values[column] for column in columns]
    elif payload.operation == "update":
        columns = list(payload.values)
        assignments = ", ".join(f"{_quote(column)} = ?" for column in columns)
        clause, where_parameters = _where_clause(payload.where)
        sql = f"UPDATE {_quote(payload.table)} SET {assignments} WHERE {clause}"
        parameters = [payload.values[column] for column in columns] + where_parameters
    else:
        clause, parameters = _where_clause(payload.where)
        sql = f"DELETE FROM {_quote(payload.table)} WHERE {clause}"
    return int(connection.execute(sql, parameters).rowcount)


def execute_sql_write(
    payload: SqlWriteInput,
    context: ToolExecutionContext,
    database_path: Path = DEMO_DB,
) -> dict[str, Any]:
    """Revalidate inside a write lock, execute once, and commit atomically."""

    if context.resource_type and context.resource_type != "table":
        raise ToolRuntimeError("approval_mismatch", "审批资源类型与 SQL 写入目标不一致。")
    if context.resource_id and context.resource_id != payload.table:
        raise ToolRuntimeError("approval_mismatch", "审批目标表与当前写入目标不一致。")
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE")
        schema = _schema(connection, payload.table)
        _validate_shape(payload, schema)
        current_rows = 1 if payload.operation == "insert" else _count_matches(connection, payload)
        if current_rows != payload.estimated_rows:
            raise ToolRuntimeError(
                "approval_stale",
                f"审批时预计影响 {payload.estimated_rows} 行，当前为 {current_rows} 行；数据已变化，请重新审批。",
            )
        affected = _execute_statement(connection, payload)
        if affected != payload.estimated_rows:
            raise ToolRuntimeError(
                "write_result_mismatch",
                f"预计影响 {payload.estimated_rows} 行，实际为 {affected} 行，事务已回滚。",
            )
        connection.commit()
        return {
            "accepted": True,
            "message": f"已在本地演示库完成 {payload.operation.upper()}，影响 {affected} 行。",
            "operation": payload.operation,
            "table": payload.table,
            "affected_rows": affected,
        }
    except ToolRuntimeError:
        connection.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise ToolRuntimeError("write_conflict", f"写入违反数据约束，事务已回滚：{exc}") from exc
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        raise ToolRuntimeError("write_failed", f"本地演示库写入失败，事务已回滚：{exc}") from exc
    finally:
        connection.close()
