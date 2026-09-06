"""Safe on-disk storage for original knowledge document uploads."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from backend.core import DATA_DIR
from backend.knowledge.models import DocumentIngestionError


MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_UPLOAD_ROOT = DATA_DIR / "uploads"
DEFAULT_OWNER_QUOTA_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_KNOWLEDGE_BASE_QUOTA_BYTES = 1024 * 1024 * 1024
_SAFE_EXTENSION = re.compile(r"^\.[a-z0-9]{1,12}$")


@dataclass(frozen=True, slots=True)
class StoredSource:
    storage_path: str
    byte_size: int
    sha256: str


def source_fingerprint(data: bytes) -> tuple[int, str]:
    if not isinstance(data, bytes):
        raise DocumentIngestionError("invalid_payload", "文档内容必须是二进制数据。")
    if not data:
        raise DocumentIngestionError("no_readable_content", "文档内容为空。")
    size = len(data)
    if size > MAX_FILE_BYTES:
        raise DocumentIngestionError(
            "file_too_large",
            "文件超过 20 MB 限制。",
            details={"size": size, "max_size": MAX_FILE_BYTES},
        )
    return size, hashlib.sha256(data).hexdigest()


class SourceStorage:
    """Store bytes beneath a generated path; user filenames are metadata only."""

    def __init__(self, root: Path = DEFAULT_UPLOAD_ROOT) -> None:
        self.root = Path(root).resolve()

    @staticmethod
    def _namespace(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _extension(filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        return suffix if _SAFE_EXTENSION.fullmatch(suffix) else ".bin"

    def destination(
        self,
        *,
        owner_id: str | None,
        knowledge_base_id: str = "__unscoped__",
        document_id: str,
        filename: str,
    ) -> Path:
        owner_namespace = self._namespace(owner_id or "__shared__")
        base_namespace = self._namespace(knowledge_base_id)
        document_namespace = self._namespace(document_id)
        path = (
            self.root
            / owner_namespace
            / base_namespace
            / document_namespace
            / f"original{self._extension(filename)}"
        )
        resolved = path.resolve()
        if self.root != resolved and self.root not in resolved.parents:
            raise RuntimeError("Upload destination escaped the configured storage root")
        return resolved

    def store(
        self,
        *,
        owner_id: str | None,
        knowledge_base_id: str = "__unscoped__",
        document_id: str,
        filename: str,
        data: bytes,
    ) -> StoredSource:
        byte_size, digest = source_fingerprint(data)
        destination = self.destination(
            owner_id=owner_id,
            knowledge_base_id=knowledge_base_id,
            document_id=document_id,
            filename=filename,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return StoredSource(
            storage_path=str(destination.relative_to(self.root.parent)),
            byte_size=byte_size,
            sha256=digest,
        )

    def remove(self, storage_path: str) -> None:
        """Best-effort cleanup restricted to this storage root."""
        if not storage_path:
            return
        candidate = (self.root.parent / storage_path).resolve()
        if self.root != candidate and self.root not in candidate.parents:
            raise ValueError("Refusing to remove a source outside the upload root")
        if candidate.is_file():
            candidate.unlink()
        parent = candidate.parent
        while parent != self.root and self.root in parent.parents:
            if not parent.exists() or any(parent.iterdir()):
                break
            parent.rmdir()
            parent = parent.parent
