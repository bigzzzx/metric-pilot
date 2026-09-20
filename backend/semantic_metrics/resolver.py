"""Explicit aliases resolve identity; Wiki recall alone cannot select a definition."""
import re
from backend.semantic_metrics.models import MetricResolution
from backend.semantic_metrics.repository import metric_records
from backend.query_fast_path import has_explicit_physical_field_intent


def resolve_metric_query(question, user_id):
    if has_explicit_physical_field_intent(question):
        return MetricResolution(status='unmatched')
    records = metric_records(user_id)
    hits = []
    for metric in records:
        for alias in {metric.name,metric.display_name,*metric.aliases}:
            if not alias:
                continue
            pattern = re.escape(alias)
            if alias.isascii():
                pattern = r'(?<![a-zA-Z0-9_])'+pattern+r'(?![a-zA-Z0-9_])'
            for match in re.finditer(pattern,question,re.I):
                hits.append((match.start(),match.end(),metric))
    # Suppress shorter aliases contained inside a more specific mention only.
    hits = [h for h in hits if not any(other[0]<=h[0] and other[1]>=h[1]
            and other[1]-other[0]>h[1]-h[0] for other in hits)]
    if not hits:
        return MetricResolution(status='unmatched')
    selected = list({h[2].id:h[2] for h in hits}.values())
    if len(selected)>1:
        return MetricResolution(status='ambiguous',candidates=[m.display_name for m in selected],
            message='请先选择一个指标口径：'+'、'.join(m.display_name+'（'+m.description+'）' for m in selected))
    metric=selected[0]
    if not metric.can_query:
        return MetricResolution(status='denied',message='缺少所选指标底层资产的 query_data 查询权限，请向管理员申请。')
    if metric.certification_status!='certified' or not metric.contract or metric.contract.status!='active':
        return MetricResolution(status='unsupported',message='所选指标尚无已认证、已启用的执行契约，请联系指标负责人完成配置。')
    return MetricResolution(status='matched',metrics=[metric])


def resolve_metric_references(references, user_id):
    """Validate every planner citation before governed compilation.

    The repository is permission scoped, so a missing and an inaccessible
    identity deliberately share the same non-disclosing denial. A bad citation
    is never dropped in order to continue with the remaining SQL.
    """

    refs = [
        (
            str(ref.get('id') if isinstance(ref, dict) else ref.id),
            str(ref.get('version') if isinstance(ref, dict) else ref.version),
        )
        for ref in references
    ]
    if not refs:
        return MetricResolution(status='unmatched')
    visible = {metric.document_id: metric for metric in metric_records(user_id)}
    resolved = []
    for document_id, version in refs:
        metric = visible.get(document_id)
        if metric is None:
            return MetricResolution(
                status='denied',
                message='引用的指标不存在、未发布或当前无权访问，已停止执行；不会回退到 Text-to-SQL。',
            )
        if metric.version != version:
            return MetricResolution(
                status='unsupported',
                message='引用的指标版本已变化或不明确，请重新获取当前口径后再查询。',
            )
        if not metric.can_query:
            return MetricResolution(
                status='denied',
                message='缺少引用指标底层资产的 query_data 查询权限，已停止执行。',
            )
        if metric.certification_status != 'certified':
            return MetricResolution(
                status='unsupported',
                message='引用的指标尚未认证，不能用于执行查询。',
            )
        if not metric.contract or metric.contract.status != 'active':
            return MetricResolution(
                status='unsupported',
                message='引用的指标没有已启用的执行契约，不能用于执行查询。',
            )
        resolved.append(metric)
    unique = {metric.document_id: metric for metric in resolved}
    if len(unique) != 1:
        return MetricResolution(
            status='ambiguous',
            candidates=[metric.display_name for metric in unique.values()],
            message='一次查询引用了多个指标口径，请先明确唯一指标后再执行。',
        )
    return MetricResolution(status='matched', metrics=list(unique.values()))
