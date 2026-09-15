"""Explicit aliases resolve identity; Wiki recall alone cannot select a definition."""
import re
from backend.semantic_metrics.models import MetricResolution
from backend.semantic_metrics.repository import metric_records


def resolve_metric_query(question, user_id):
    if any(word in question for word in ('物理列','原始字段','表中现有字段','不套用指标口径')):
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
