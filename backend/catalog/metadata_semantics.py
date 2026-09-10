"""Parse governed semantic hints already present in source comments."""
from __future__ import annotations

import re


DOMAIN_IDS = {
    "电商": "domain-ecommerce",
    "交易": "domain-ecommerce",
    "营销": "domain-marketing",
    "供应链": "domain-supply-chain",
}
DOMAIN_OWNER_GROUPS = {
    "domain-ecommerce": "group-ecommerce-analysts",
    "domain-marketing": "group-marketing-analysts",
    "domain-supply-chain": "group-supply-chain-analysts",
}
WAREHOUSE_LAYERS = {"ODS", "DWD", "DIM", "DWS", "ADS"}


def comment_attributes(comment: str) -> dict[str, str]:
    """Read `key=value` pairs separated by Chinese or ASCII semicolons."""
    attributes: dict[str, str] = {}
    for segment in re.split(r"[;；\n]+", str(comment or "")):
        key, separator, value = segment.partition("=")
        if separator and key.strip() and value.strip():
            attributes[key.strip()] = value.strip()
    return attributes


def _values(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，、|]+", value or "") if item.strip()]


def table_semantics(comment: str, namespace: str) -> dict:
    attributes = comment_attributes(comment)
    requested_layer = attributes.get("分层", namespace).upper()
    layer = requested_layer if requested_layer in WAREHOUSE_LAYERS else "UNKNOWN"
    domain_id = DOMAIN_IDS.get(attributes.get("业务域", ""), "domain-ecommerce")
    return {
        "domain_id": domain_id,
        "owner_group_id": DOMAIN_OWNER_GROUPS[domain_id],
        "warehouse_layer": layer,
        "data_grain": attributes.get("粒度", ""),
        "aliases": _values(attributes.get("业务别名", attributes.get("别名", ""))),
        "sensitivity": "sensitive" if attributes.get("敏感字段") else "internal",
    }


def column_semantics(comment: str, physical_name: str, data_type: str, is_primary_key: bool) -> dict:
    attributes = comment_attributes(comment)
    aliases = _values(attributes.get("业务别名", attributes.get("别名", "")))
    sensitivity_label = attributes.get("敏感级别", "").lower()
    sensitivity = {
        "公开": "public", "低": "internal", "中": "internal", "高": "sensitive",
        "敏感": "sensitive", "受限": "restricted", "restricted": "restricted",
        "sensitive": "sensitive", "public": "public", "internal": "internal",
    }.get(sensitivity_label, "internal")
    display_rule = attributes.get("展示规则", "") + str(comment or "")
    masking = "none"
    if sensitivity in {"sensitive", "restricted"}:
        masking = "partial" if any(token in display_rule for token in ("仅保留", "隐藏")) else "redact"
    lowered_type = str(data_type or "").lower()
    if is_primary_key:
        semantic_role = "identifier"
    elif "date" in lowered_type or "time" in lowered_type:
        semantic_role = "time"
    elif any(token in str(comment or "") for token in ("金额", "数量", "花费", "收入", "成本", "比例", "率")):
        semantic_role = "measure"
    else:
        semantic_role = "attribute"
    classification = {
        "customer_name": "person_name",
        "phone": "phone_number",
        "email": "email_address",
    }.get(physical_name, "")
    return {
        "display_name": aliases[0] if aliases else physical_name,
        "aliases": aliases,
        "semantic_role": semantic_role,
        "sensitivity": sensitivity,
        "classification": classification,
        "masking_strategy": masking,
    }
