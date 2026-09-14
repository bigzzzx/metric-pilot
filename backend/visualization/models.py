from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class VisualizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chart_type: Literal["auto", "table", "card", "line", "bar", "horizontal_bar", "pie", "scatter"] = "auto"
    x: str | None = Field(default=None, max_length=120)
    y: list[str] = Field(default_factory=list, max_length=3)
    title: str = Field(default="", max_length=160)


class VisualizationInput(VisualizationRequest):
    result_id: str = Field(min_length=1, max_length=80)


class VisualizationOutput(BaseModel):
    result_id: str
    chart_type: Literal["table", "card", "line", "bar", "horizontal_bar", "pie", "scatter"]
    title: str
    x: str | None = None
    y: list[str] = Field(default_factory=list)
    row_count: int
    available_types: list[str] = Field(default_factory=list)
    notice: str = ""
