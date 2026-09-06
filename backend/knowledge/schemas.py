"""Pydantic contracts at the knowledge-base HTTP boundary."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


DocumentStatus = Literal["processing", "ready", "failed", "ocr_required"]


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)

    @field_validator("name", "description")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("name")
    @classmethod
    def reject_blank_name(cls, value: str) -> str:
        if not value:
            raise ValueError("知识库名称不能为空。")
        return value


class KnowledgeTextDocumentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    content: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    source_type: str = Field(default="txt", min_length=1, max_length=40)

    @field_validator("name", "content", "source_type")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("name", "content")
    @classmethod
    def reject_blank_content(cls, value: str) -> str:
        if not value:
            raise ValueError("文档名称和内容不能为空。")
        return value


class KnowledgeDocumentView(BaseModel):
    id: str
    knowledge_base_id: str
    name: str
    source_type: str
    media_type: str | None = None
    byte_size: int = 0
    sha256: str = ""
    status: DocumentStatus = "ready"
    parser_name: str | None = None
    parser: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    chunk_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    model_config = ConfigDict(extra="forbid")


class KnowledgeImportResponse(BaseModel):
    document: KnowledgeDocumentView
    document_id: str
    status: DocumentStatus
    parser: str | None = None
    chunk_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    error_code: str | None = None
    index: Literal["milvus-bge-hybrid", "sqlite-local-hybrid"]
    message: str
    request_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class KnowledgeBaseView(BaseModel):
    id: str
    name: str
    description: str = ""
    created_at: str | None = None
    document_count: int = 0
    owner_id: str | None = None
    can_write: bool

    model_config = ConfigDict(extra="forbid")
