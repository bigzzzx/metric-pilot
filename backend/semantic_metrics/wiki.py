"""Traceable metric Wiki drafts, review, publication, and index refresh."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import Field

from backend.catalog.service import DISCOVER, MANAGE, can_access
from backend.core import db
from backend.model_client import complete_structured
from backend.semantic_metrics.compiler import validate_expression
from backend.semantic_metrics.models import ContractModel, MetricContract


class WikiClaim(ContractModel):
    claim_type: Literal["definition", "filter", "time", "grain", "dimension", "difference", "warning"]
    content: str = Field(min_length=1, max_length=1000)
    source_ref: str = Field(min_length=1, max_length=256)
    evidence: str = Field(min_length=1, max_length=2000)


class WikiDraft(ContractModel):
    summary: str = Field(max_length=1000)
    content: str = Field(min_length=1, max_length=6000)
    related_metric_ids: list[str] = Field(default_factory=list, max_length=10)
    claims: list[WikiClaim] = Field(min_length=1, max_length=30)


class MetricReview(ContractModel):
    expected_version: int = Field(ge=1)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    contract: MetricContract
    wiki: WikiDraft


class WikiSourceInput(ContractModel):
    source_type: Literal[
        "catalog_snapshot", "business_document", "sql_definition",
        "meeting_record", "knowledge_document", "manual",
    ] = "business_document"
    title: str = Field(min_length=1, max_length=300)
    source_uri: str = Field(default="", max_length=1000)
    content: str = Field(min_length=1, max_length=30000)


def require_admin(con, user_id):
    if not can_access(user_id, MANAGE, connection=con):
        raise HTTPException(403, "缺少 manage 目录管理权限。")


def _source_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _source_view(row, *, include_content: bool = True) -> dict:
    item = dict(row)
    if not include_content:
        item.pop("content", None)
    return item


def source_records(con, metric_id: str, *, include_content: bool = True) -> list[dict]:
    return [
        _source_view(row, include_content=include_content)
        for row in con.execute(
            "SELECT * FROM metric_wiki_sources WHERE metric_id=? ORDER BY created_at,id",
            (metric_id,),
        )
    ]


def _insert_source(con, metric_id: str, user_id: str, payload: WikiSourceInput, *, source_id: str | None = None) -> dict:
    content = payload.content.strip()
    digest = _source_hash(content)
    existing = con.execute(
        "SELECT * FROM metric_wiki_sources WHERE metric_id=? AND content_hash=?",
        (metric_id, digest),
    ).fetchone()
    if existing:
        return _source_view(existing)
    identifier = source_id or f"wiki-source:{metric_id}:{digest[:20]}"
    con.execute(
        """INSERT INTO metric_wiki_sources(
            id,metric_id,source_type,title,source_uri,content,content_hash,created_by)
            VALUES (?,?,?,?,?,?,?,?)""",
        (identifier, metric_id, payload.source_type, payload.title.strip(),
         payload.source_uri.strip(), content, digest, user_id),
    )
    con.execute(
        """INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json)
           VALUES (?,'metric_wiki_source_added','metric',?,?)""",
        (user_id, metric_id, json.dumps({"source_id": identifier, "content_hash": digest}, ensure_ascii=False)),
    )
    return _source_view(con.execute("SELECT * FROM metric_wiki_sources WHERE id=?", (identifier,)).fetchone())


def add_source(metric_id: str, user_id: str, payload: WikiSourceInput) -> dict:
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        require_admin(con, user_id)
        if not con.execute("SELECT 1 FROM catalog_metrics WHERE id=?", (metric_id,)).fetchone():
            raise HTTPException(404, "Metric not found")
        result = _insert_source(con, metric_id, user_id, payload)
        con.commit()
        return result
    finally:
        con.close()


def _catalog_snapshot(row) -> tuple[str, WikiSourceInput]:
    item = dict(row)
    source_id = f"wiki-source:{item['id']}:catalog-v{item['catalog_version']}"
    content = json.dumps({
        "metric_id": item["id"],
        "display_name": item["display_name"],
        "description": item["description"],
        "formula": item["formula"],
        "definition_sql": item["definition_sql"],
        "grain": item["grain"],
        "catalog_version": item["catalog_version"],
        "contract": json.loads(item.get("contract_json") or "{}"),
    }, ensure_ascii=False, sort_keys=True)
    return source_id, WikiSourceInput(
        source_type="catalog_snapshot",
        title=f"{item['display_name']} catalog v{item['catalog_version']}",
        source_uri=f"catalog:metric:{item['id']}@{item['catalog_version']}",
        content=content,
    )


def _validate_claims(con, metric_id: str, claims: list[WikiClaim]) -> list[str]:
    sources = {
        str(row["id"]): str(row["content"])
        for row in con.execute("SELECT id,content FROM metric_wiki_sources WHERE metric_id=?", (metric_id,))
    }
    refs: list[str] = []
    for claim in claims:
        source = sources.get(claim.source_ref)
        if source is None:
            raise HTTPException(422, f"Wiki 声明引用了不存在的原始来源：{claim.source_ref}")
        if claim.evidence.strip() not in source:
            raise HTTPException(422, f"Wiki 声明的证据不是来源原文片段：{claim.source_ref}")
        if claim.source_ref not in refs:
            refs.append(claim.source_ref)
    return refs


def visible_wiki(con, row, user_id):
    wiki = json.loads(dict(row).get("wiki_json") or "{}")
    if wiki.get("status") != "approved":
        return {}
    for identifier in wiki.get("related_metric_ids", []):
        other = con.execute(
            "SELECT * FROM catalog_metrics WHERE id=? AND publish_status='published'", (identifier,)
        ).fetchone()
        if not other or not can_access(user_id, DISCOVER, domain_id=other["domain_id"], connection=con):
            return {}
        contract = json.loads(other["contract_json"] or "{}")
        if contract.get("asset_id") and not can_access(
            user_id, DISCOVER, asset_id=contract["asset_id"], connection=con
        ):
            return {}
    for claim in wiki.get("claims", []):
        source = con.execute(
            "SELECT 1 FROM metric_wiki_sources WHERE id=? AND metric_id=?",
            (claim.get("source_ref"), row["id"]),
        ).fetchone()
        if not source:
            return {}
    return wiki


async def generate_draft(metric_id, user_id):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        require_admin(con, user_id)
        row = con.execute("SELECT * FROM catalog_metrics WHERE id=?", (metric_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Metric not found")
        source_id, snapshot = _catalog_snapshot(row)
        _insert_source(con, metric_id, user_id, snapshot, source_id=source_id)
        sources = source_records(con, metric_id)
        related = [
            dict(item) for item in con.execute(
                "SELECT id,display_name,description FROM catalog_metrics WHERE domain_id=? AND id!=? LIMIT 10",
                (row["domain_id"], metric_id),
            )
        ]
        source_version = row["catalog_version"]
        con.commit()
    finally:
        con.close()

    budget = 40000
    supplied = []
    for source in sources:
        excerpt = source["content"][: min(len(source["content"]), budget)]
        if not excerpt:
            continue
        supplied.append({
            "id": source["id"], "type": source["source_type"], "title": source["title"],
            "uri": source["source_uri"], "content": excerpt,
        })
        budget -= len(excerpt)
        if budget <= 0:
            break
    prompt = (
        "Write a Chinese metric Wiki draft grounded ONLY in supplied immutable sources. "
        "Explain filters, time field, grain, scenarios and differences. Identify conflicts explicitly. "
        "Every factual claim MUST cite one supplied source id and copy a short verbatim evidence substring "
        "from that source. Do not invent facts, contracts or approval. related_metric_ids may contain only "
        "supplied related IDs. Return summary, content, related_metric_ids and claims.\n"
        + json.dumps({"sources": supplied, "related": related}, ensure_ascii=False)
    )
    draft = await complete_structured(
        "data-query-agent",
        [{"role": "system", "content": prompt}, {"role": "user", "content": "生成待审核口径 Wiki"}],
        WikiDraft,
        None,
    )
    if set(draft.related_metric_ids) - {item["id"] for item in related}:
        raise HTTPException(422, "Wiki 引用了未提供的指标。")
    validation_error = None
    for attempt in range(2):
        con = db()
        try:
            require_admin(con, user_id)
            try:
                source_refs = _validate_claims(con, metric_id, draft.claims)
            except HTTPException as exc:
                validation_error = exc
            else:
                source_meta = [
                    {"id": item["id"], "content_hash": item["content_hash"], "source_uri": item["source_uri"]}
                    for item in source_records(con, metric_id, include_content=False)
                    if item["id"] in source_refs
                ]
                saved = {
                    **draft.model_dump(), "source_refs": source_refs, "source_snapshot": source_meta,
                    "status": "draft",
                }
                changed = con.execute(
                    "UPDATE catalog_metrics SET wiki_draft_json=? WHERE id=? AND catalog_version=?",
                    (json.dumps(saved, ensure_ascii=False), metric_id, source_version),
                ).rowcount
                if changed != 1:
                    raise HTTPException(409, "指标已变化，请重新生成 Wiki。")
                con.commit()
                return saved
        finally:
            con.close()
        if attempt == 0:
            repair_prompt = (
                prompt + "\nThe previous draft failed evidence validation: "
                + str(validation_error.detail)
                + "\nRepair the draft. Keep only supported claims and copy evidence character-for-character "
                  "from the content of the cited source. Return the complete WikiDraft again."
            )
            draft = await complete_structured(
                "data-query-agent",
                [{"role": "system", "content": repair_prompt},
                 {"role": "user", "content": draft.model_dump_json()}],
                WikiDraft,
                None,
            )
            if set(draft.related_metric_ids) - {item["id"] for item in related}:
                raise HTTPException(422, "Wiki 引用了未提供的指标。")
    raise validation_error


def publish(metric_id, user_id, payload):
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        require_admin(con, user_id)
        row = con.execute("SELECT * FROM catalog_metrics WHERE id=?", (metric_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Metric not found")
        if row["catalog_version"] != payload.expected_version:
            raise HTTPException(409, "指标已变化，请刷新后重新审核。")
        contract = payload.contract
        asset = con.execute(
            """SELECT a.*,s.kind source_kind FROM catalog_assets a
               JOIN data_sources s ON s.id=a.source_id
               WHERE a.id=? AND a.publish_status='published' AND s.enabled=1""",
            (contract.asset_id,),
        ).fetchone()
        if not asset or (
            asset["source_id"], asset["namespace"], asset["physical_name"], asset["domain_id"]
        ) != (contract.source_id, contract.namespace, contract.table, row["domain_id"]):
            raise HTTPException(422, "契约必须绑定同业务域的已发布资产。")
        expected_dialect = "postgresql" if asset["source_kind"] == "postgresql" else "sqlite"
        if contract.dialect != expected_dialect:
            raise HTTPException(422, "SQL 方言不匹配。")
        fields = {
            item["physical_name"] for item in con.execute(
                """SELECT physical_name FROM catalog_columns
                   WHERE asset_id=? AND sensitivity NOT IN ('sensitive','restricted')""",
                (contract.asset_id,),
            )
        }
        needed = (
            {item.column for item in contract.required_filters}
            | {dimension.column for dimension in contract.dimensions.values()}
            | ({contract.time_column} if contract.time_column else set())
        )
        if not needed <= fields or (contract.requires_time_range and not contract.time_column):
            raise HTTPException(422, "契约字段不存在或时间配置不完整。")
        try:
            validate_expression(contract, fields)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        for other in payload.wiki.related_metric_ids:
            if not con.execute(
                """SELECT 1 FROM catalog_metrics
                   WHERE id=? AND publish_status='published' AND domain_id=?""",
                (other, row["domain_id"]),
            ).fetchone():
                raise HTTPException(422, "关联指标必须已发布且属于同一业务域。")
        source_refs = _validate_claims(con, metric_id, payload.wiki.claims)
        source_meta = [
            {"id": item["id"], "content_hash": item["content_hash"], "source_uri": item["source_uri"]}
            for item in source_records(con, metric_id, include_content=False)
            if item["id"] in source_refs
        ]
        approved = {
            **payload.wiki.model_dump(), "status": "approved", "source_refs": source_refs,
            "source_snapshot": source_meta,
        }
        version = payload.expected_version + 1
        job_id = f"metric-wiki-index:{metric_id}:v{version}"
        con.execute(
            """UPDATE catalog_metrics SET aliases_json=?,contract_json=?,wiki_json=?,wiki_draft_json='{}',
               certification_status='certified',publish_status='published',catalog_version=?,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (json.dumps(payload.aliases, ensure_ascii=False), contract.model_dump_json(),
             json.dumps(approved, ensure_ascii=False), version, metric_id),
        )
        con.execute(
            """INSERT INTO metric_wiki_revisions(metric_id,version,snapshot_json,actor_id)
               VALUES (?,?,?,?)""",
            (metric_id, version, payload.model_dump_json(), user_id),
        )
        con.execute(
            """INSERT INTO metric_wiki_index_jobs(id,metric_id,metric_version,status)
               VALUES (?,?,?,'pending')""",
            (job_id, metric_id, version),
        )
        con.execute(
            """INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json)
               VALUES (?,'metric_publish','metric',?,?)""",
            (user_id, metric_id, json.dumps({
                "version": version, "source_refs": source_refs, "index_job_id": job_id,
            }, ensure_ascii=False)),
        )
        con.commit()
        return {"id": metric_id, "version": version, "status": "published",
                "index_status": "pending", "index_job_id": job_id}
    finally:
        con.close()


def run_index_job(job_id: str) -> dict:
    """Refresh the derived Milvus projection after the publication transaction."""
    con = db()
    try:
        job = con.execute("SELECT * FROM metric_wiki_index_jobs WHERE id=?", (job_id,)).fetchone()
        if not job:
            return {"id": job_id, "status": "missing"}
        if job["status"] == "completed":
            return {"id": job_id, "status": "completed"}
        con.execute(
            "UPDATE metric_wiki_index_jobs SET status='running',error='' WHERE id=?",
            (job_id,),
        )
        con.commit()
    finally:
        con.close()
    try:
        from backend.catalog.vector_index import ensure_catalog_index
        if not ensure_catalog_index():
            raise RuntimeError("catalog vector index is unavailable")
    except Exception as exc:
        con = db()
        try:
            con.execute(
                "UPDATE metric_wiki_index_jobs SET status='failed',error=? WHERE id=?",
                (str(exc)[:1000], job_id),
            )
            con.commit()
        finally:
            con.close()
        return {"id": job_id, "status": "failed", "error": str(exc)}
    con = db()
    try:
        con.execute(
            """UPDATE metric_wiki_index_jobs
               SET status='completed',error='',completed_at=CURRENT_TIMESTAMP WHERE id=?""",
            (job_id,),
        )
        con.commit()
    finally:
        con.close()
    return {"id": job_id, "status": "completed"}


def retry_index(metric_id: str, user_id: str) -> dict:
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        require_admin(con, user_id)
        job = con.execute(
            """SELECT * FROM metric_wiki_index_jobs
               WHERE metric_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
            (metric_id,),
        ).fetchone()
        if not job:
            raise HTTPException(404, "该指标没有可重试的 Wiki 索引任务。")
        if job["status"] != "completed":
            con.execute(
                "UPDATE metric_wiki_index_jobs SET status='pending',error='' WHERE id=?",
                (job["id"],),
            )
        con.execute(
            """INSERT INTO catalog_audit_logs(actor_id,action,entity_type,entity_id,detail_json)
               VALUES (?,'metric_wiki_index_retry','metric',?,?)""",
            (user_id, metric_id, json.dumps({"index_job_id": job["id"]}, ensure_ascii=False)),
        )
        con.commit()
        return {"id": job["id"], "status": "pending" if job["status"] != "completed" else "completed"}
    finally:
        con.close()
