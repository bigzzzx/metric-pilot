from typing import Literal
from pydantic import Field
from backend.tools.models import StrictModel


class EvidenceInput(StrictModel):
    source_kind: Literal['sql_import','etl_definition','business_document','external_run'] = 'sql_import'
    title: str = Field(min_length=1,max_length=200)
    content: str = Field(min_length=1,max_length=100000)
    source_id: str | None = Field(default=None,max_length=128)
    source_uri: str = Field(default='',max_length=1000)
    source_version: str = Field(default='',max_length=128)
    asset_ids: list[str] = Field(default_factory=list,max_length=12)
    run_metadata: dict = Field(default_factory=dict)


class VersionInput(StrictModel):
    expected_version: int = Field(ge=1)


class ReviewInput(VersionInput):
    decision: Literal['approve','reject']
    reason: str = Field(min_length=3,max_length=1500)
    cardinality: Literal['unknown','one_to_one','many_to_one','one_to_many','many_to_many'] = 'unknown'
    recommended_join: Literal['unspecified','INNER JOIN','LEFT JOIN','RIGHT JOIN','FULL JOIN'] = 'unspecified'


class SourceStateInput(VersionInput):
    status: Literal['active','invalidated']
    reason: str = Field(min_length=3,max_length=1000)


class DocumentClaim(StrictModel):
    kind: Literal['join','column_value','table_transform','column_influence']
    from_asset_id: str = Field(min_length=1,max_length=128)
    to_asset_id: str = Field(min_length=1,max_length=128)
    from_column: str = Field(default='',max_length=128)
    to_column: str = Field(default='',max_length=128)
    expression: str = Field(min_length=1,max_length=2000)
    block_index: int = Field(ge=0)
    quote: str = Field(min_length=5,max_length=1500)


class DocumentClaims(StrictModel):
    claims: list[DocumentClaim] = Field(default_factory=list,max_length=30)
    limitations: str = Field(default='',max_length=2000)
