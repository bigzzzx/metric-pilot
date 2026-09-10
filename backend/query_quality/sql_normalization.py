"""Deterministic SQL normalization backed only by verified schema metadata."""
from __future__ import annotations

import re

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError


_ENUM_VALUE = re.compile(r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{1,})(?![A-Za-z0-9_])")


def _documented_enums(schemas: dict) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {}
    for table in schemas.get("tables", []):
        for column in table.get("columns", []):
            documented = set(_ENUM_VALUE.findall(str(column.get("description") or "")))
            if documented:
                values.setdefault(str(column.get("name") or "").casefold(), set()).update(documented)
    return values


def _canonical_literal(column: exp.Expression, literal: exp.Expression, enums: dict[str, set[str]]):
    if not isinstance(column, exp.Column) or not isinstance(literal, exp.Literal) or not literal.is_string:
        return literal
    candidates = [
        value for value in enums.get(column.name.casefold(), set())
        if value.casefold() == str(literal.this).casefold()
    ]
    return exp.Literal.string(candidates[0]) if len(candidates) == 1 else literal


def canonicalize_documented_enums(sql: str, schemas: dict) -> str:
    """Match string-literal casing to enum values explicitly documented in schema."""
    enums = _documented_enums(schemas)
    if not enums:
        return sql
    dialect = next((table.get("dialect") for table in schemas.get("tables", []) if table.get("dialect")), None)
    dialect = "postgres" if dialect == "postgresql" else dialect
    try:
        expression = parse_one(sql, read=dialect)
    except (ParseError, ValueError):
        return sql
    changed = False
    for comparison in expression.find_all(exp.EQ):
        replacement = _canonical_literal(comparison.this, comparison.expression, enums)
        changed = changed or replacement is not comparison.expression
        comparison.set("expression", replacement)
    for predicate in expression.find_all(exp.In):
        if isinstance(predicate.this, exp.Column):
            replacements = [
                _canonical_literal(predicate.this, literal, enums) for literal in predicate.expressions
            ]
            changed = changed or any(
                replacement is not literal
                for replacement, literal in zip(replacements, predicate.expressions)
            )
            predicate.set(
                "expressions",
                replacements,
            )
    return expression.sql(dialect=dialect) if changed else sql
