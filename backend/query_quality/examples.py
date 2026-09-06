"""Only explicitly curated examples with unchanged, authorized evidence are reused."""
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.core import ROOT
from backend.catalog.rag_documents import authorized_catalog_documents
from backend.hybrid_retrieval import bm25_rank
from backend.query_quality.evidence import fingerprint
from backend.observability import log_event

EXAMPLES_FILE = ROOT / "metadata" / "query_examples.json"


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    version: str = Field(pattern=r"^[0-9a-f]{64}$")


class VerifiedExample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    question: str
    sql: str
    validation: str
    evidence: list[EvidenceReference] = Field(min_length=1)


def retrieve_examples(query: str, user_id: str, limit: int = 3) -> list[dict]:
    if not user_id or not EXAMPLES_FILE.exists():
        return []
    current = {item["id"]: fingerprint(item) for item in authorized_catalog_documents(user_id)}
    candidates = []
    try:
        records = json.loads(EXAMPLES_FILE.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError("Examples must be a list")
    except (OSError, ValueError):
        log_event("query_examples.unavailable", level=30)
        return []
    for raw in records:
        try:
            item = VerifiedExample.model_validate(raw)
        except ValidationError:
            log_event("query_examples.invalid_record", level=30)
            continue
        if not item.validation.strip() or not all(current.get(ref.id) == ref.version for ref in item.evidence):
            continue
        candidates.append({"id": item.id, "content": item.question, "question": item.question,
                           "sql": item.sql, "validation": item.validation})
    ranked = bm25_rank(query, candidates, max(0, min(limit, 3)))
    return [{key: item[key] for key in ("id", "question", "sql", "validation")} for item in ranked]
