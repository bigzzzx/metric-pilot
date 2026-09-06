"""JSON and YAML parsers preserving JSONPath-style object locations."""

from __future__ import annotations

import json
from typing import Any

from backend.knowledge.limits import DEFAULT_LIMITS, IngestionLimits, ParsedContentBudget
from backend.knowledge.models import DocumentBlock, DocumentIngestionError, ParsedDocument, dependency_missing
from backend.knowledge.parsers.base import decode_text, document_title, require_readable


def _records(value: Any, *, limits: IngestionLimits, path: str = "$") -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    budget = ParsedContentBudget(limits)
    _walk_records(value, path=path, depth=1, blocks=blocks, budget=budget, limits=limits)
    return blocks


def _walk_records(
    value: Any,
    *,
    path: str,
    depth: int,
    blocks: list[DocumentBlock],
    budget: ParsedContentBudget,
    limits: IngestionLimits,
) -> None:
    if depth > limits.max_structured_depth:
        raise DocumentIngestionError(
            "structured_depth_limit_exceeded",
            "结构化文档的嵌套深度超过安全限制。",
            details={"actual": depth, "limit": limits.max_structured_depth},
        )

    def emit(text: str, item_path: str) -> None:
        budget.append(blocks, DocumentBlock("record", text, locator={"path": item_path}))

    if isinstance(value, dict):
        if not value:
            emit(_bounded_text(("{}",), budget), path)
            return
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if isinstance(child, list):
                if not child:
                    emit(_bounded_text((str(key), ": []"), budget), child_path)
                for index, item in enumerate(child):
                    item_path = f"{child_path}[{index}]"
                    if isinstance(item, (dict, list)):
                        _ensure_structure_depth(item, limits.max_structured_depth - depth)
                        text = _bounded_json_text(item, budget)
                    else:
                        text = _bounded_text((str(key), "[", str(index), "]: ", str(item)), budget)
                    emit(text, item_path)
            elif isinstance(child, dict):
                _walk_records(
                    child,
                    path=child_path,
                    depth=depth + 1,
                    blocks=blocks,
                    budget=budget,
                    limits=limits,
                )
            else:
                emit(_bounded_text((str(key), ": ", str(child)), budget), child_path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            item_path = f"{path}[{index}]"
            if isinstance(item, (dict, list)):
                _ensure_structure_depth(item, limits.max_structured_depth - depth)
                text = _bounded_json_text(item, budget)
            else:
                text = _bounded_text((str(item),), budget)
            emit(text, item_path)
        return
    emit(_bounded_text((str(value),), budget), path)


def _bounded_json_text(value: Any, budget: ParsedContentBudget) -> str:
    encoder = json.JSONEncoder(ensure_ascii=False, indent=2, default=str)
    return _bounded_text(encoder.iterencode(value), budget)


def _bounded_text(fragments, budget: ParsedContentBudget) -> str:
    parts: list[str] = []
    size = 0
    block_limit = budget.limits.max_block_characters
    remaining_total = budget.limits.max_document_characters - budget.total_characters
    for fragment in fragments:
        size += len(fragment)
        if size > block_limit:
            raise DocumentIngestionError(
                "document_block_too_large",
                "解析结果中的单个内容块过大。",
                details={"block_index": budget.block_count, "actual": size, "limit": block_limit},
            )
        if size > remaining_total:
            raise DocumentIngestionError(
                "document_character_limit_exceeded",
                "解析结果的文本总量超过限制。",
                details={
                    "actual": budget.total_characters + size,
                    "limit": budget.limits.max_document_characters,
                },
            )
        parts.append(fragment)
    return "".join(parts)


def _ensure_structure_depth(value: Any, max_depth: int) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > max_depth:
            raise DocumentIngestionError(
                "structured_depth_limit_exceeded",
                "结构化文档的嵌套深度超过安全限制。",
                details={"actual": depth, "limit": max_depth},
            )
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values() if isinstance(child, (dict, list)))
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item if isinstance(child, (dict, list)))


class JsonParser:
    name = "json"
    extensions = frozenset({"json"})
    media_types = frozenset({"application/json", "text/json"})

    def __init__(self, *, limits: IngestionLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument:
        try:
            payload = json.loads(decode_text(data, parser_name=self.name))
        except json.JSONDecodeError as exc:
            raise DocumentIngestionError(
                "parse_failed",
                f"JSON 格式错误（第 {exc.lineno} 行，第 {exc.colno} 列）。",
                details={"line": exc.lineno, "column": exc.colno, "parser": self.name},
            ) from exc
        except RecursionError as exc:
            raise DocumentIngestionError(
                "structured_depth_limit_exceeded",
                "JSON 嵌套深度超过安全限制。",
                details={"limit": self.limits.max_structured_depth},
            ) from exc
        blocks = tuple(_records(payload, limits=self.limits))
        require_readable(len(blocks), self.name)
        return ParsedDocument(document_title(filename), "json", blocks, {"media_type": media_type or "application/json"})


class YamlParser:
    name = "yaml"
    extensions = frozenset({"yaml", "yml"})
    media_types = frozenset({"application/yaml", "application/x-yaml", "text/yaml", "text/x-yaml"})

    def __init__(self, *, limits: IngestionLimits = DEFAULT_LIMITS) -> None:
        self.limits = limits

    def parse(self, filename: str, media_type: str | None, data: bytes) -> ParsedDocument:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise dependency_missing("PyYAML", self.name) from exc
        text = decode_text(data, parser_name=self.name)
        try:
            _validate_yaml_events(yaml, text, self.limits)
            payload = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise DocumentIngestionError("parse_failed", "YAML 格式错误。", details={"parser": self.name}) from exc
        if payload is None:
            require_readable(0, self.name)
        payload = _materialize_yaml(payload, limits=self.limits)
        blocks = tuple(_records(payload, limits=self.limits))
        require_readable(len(blocks), self.name)
        return ParsedDocument(document_title(filename), "yaml", blocks, {"media_type": media_type or "application/yaml"})


def _validate_yaml_events(yaml, text: str, limits: IngestionLimits) -> None:
    from yaml.events import AliasEvent, MappingEndEvent, MappingStartEvent, ScalarEvent, SequenceEndEvent, SequenceStartEvent

    aliases = 0
    nodes = 0
    depth = 0
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        if isinstance(event, AliasEvent):
            aliases += 1
            if aliases > limits.max_yaml_aliases:
                raise DocumentIngestionError(
                    "yaml_alias_limit_exceeded",
                    "YAML alias 数量超过安全限制。",
                    details={"actual": aliases, "limit": limits.max_yaml_aliases},
                )
        if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            nodes += 1
            depth += 1
            if depth > limits.max_yaml_depth:
                raise DocumentIngestionError(
                    "yaml_depth_limit_exceeded",
                    "YAML 嵌套深度超过安全限制。",
                    details={"actual": depth, "limit": limits.max_yaml_depth},
                )
        elif isinstance(event, ScalarEvent):
            nodes += 1
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            depth = max(0, depth - 1)
        if nodes > limits.max_yaml_nodes:
            raise DocumentIngestionError(
                "yaml_node_limit_exceeded",
                "YAML 节点数量超过安全限制。",
                details={"actual": nodes, "limit": limits.max_yaml_nodes},
            )


def _materialize_yaml(value: Any, *, limits: IngestionLimits) -> Any:
    expanded_nodes = 0

    def visit(item: Any, ancestry: frozenset[int], depth: int) -> Any:
        nonlocal expanded_nodes
        expanded_nodes += 1
        if expanded_nodes > limits.max_yaml_expanded_nodes:
            raise DocumentIngestionError(
                "yaml_expansion_limit_exceeded",
                "YAML alias 展开后的节点数量超过安全限制。",
                details={"actual": expanded_nodes, "limit": limits.max_yaml_expanded_nodes},
            )
        if depth > limits.max_yaml_depth:
            raise DocumentIngestionError(
                "yaml_depth_limit_exceeded",
                "YAML 展开后的嵌套深度超过安全限制。",
                details={"actual": depth, "limit": limits.max_yaml_depth},
            )
        if isinstance(item, (dict, list)):
            identity = id(item)
            if identity in ancestry:
                raise DocumentIngestionError(
                    "yaml_recursive_alias",
                    "YAML 包含递归 alias，无法安全展开。",
                )
            ancestry = ancestry | {identity}
        if isinstance(item, dict):
            return {str(key): visit(child, ancestry, depth + 1) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child, ancestry, depth + 1) for child in item]
        return item

    return visit(value, frozenset(), 1)
