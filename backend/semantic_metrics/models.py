"""Strict data contracts for governed metrics.

The wiki text helps people and retrieval.  These models are the executable
contract and are deliberately much smaller than a general semantic language.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import re


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MetricFilter(ContractModel):
    column: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    operator: Literal["=", "!=", ">", ">=", "<", "<="] = "="
    value: str | int | float | bool


class MetricDimension(ContractModel):
    column: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    label: str = ""
    aliases: list[str] = Field(default_factory=list, max_length=20)
    kind: Literal["attribute", "time"] = "attribute"


class MetricContract(ContractModel):
    contract_version: int = Field(default=1, ge=1)
    status: Literal["active", "disabled"] = "active"
    source_id: str = Field(min_length=1, max_length=128)
    asset_id: str = Field(min_length=1, max_length=128)
    namespace: str = Field(default="", max_length=128)
    table: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    dialect: Literal["sqlite", "postgresql"] = "sqlite"
    measure_expression: str = Field(min_length=1, max_length=500)
    measure_alias: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    time_column: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    requires_time_range: bool = True
    required_filters: list[MetricFilter] = Field(default_factory=list, max_length=20)
    dimensions: dict[str, MetricDimension] = Field(default_factory=dict)

    @model_validator(mode='after')
    def validate_dimension_names(self):
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',key) or key==self.measure_alias for key in self.dimensions):
            raise ValueError('Dimensions must use unique SQL identifiers distinct from the measure alias')
        return self


class GovernedMetric(ContractModel):
    id: str
    document_id: str
    version: str
    name: str
    display_name: str
    domain_id: str
    description: str
    aliases: list[str]
    certification_status: str
    contract: MetricContract | None = None
    can_query: bool = True
    wiki: dict = Field(default_factory=dict)


class MetricResolution(ContractModel):
    status: Literal["matched", "unmatched", "ambiguous", "denied", "unsupported"]
    message: str = ""
    metrics: list[GovernedMetric] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)
