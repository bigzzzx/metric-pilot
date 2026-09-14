"""The executor can only resolve the result bound by this graph invocation."""
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar

from backend.tools.errors import ToolRuntimeError
from backend.tools.models import ToolSpec
from backend.visualization.models import VisualizationInput, VisualizationOutput
from backend.visualization.service import build_visualization

_trusted_result = ContextVar("visualization_trusted_result", default=None)


@contextmanager
def bind_result(rows, context):
    ident = hashlib.sha256(json.dumps([context.user_id, context.conversation_id, rows],
                                     ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    token = _trusted_result.set((ident, context.user_id, context.conversation_id, rows))
    try:
        yield ident
    finally:
        _trusted_result.reset(token)


def create_visualization(payload, context):
    result = _trusted_result.get()
    if not result or result[:3] != (payload.result_id, context.user_id, context.conversation_id):
        raise ToolRuntimeError("result_unavailable", "当前用户和会话没有可用的对应查询结果。")
    return build_visualization(result[3], payload, payload.result_id).model_dump()


SPEC = ToolSpec(id="create_visualization", name="查询结果可视化",
                description="用当前可信查询结果生成指标卡、折线、柱状、条形、环形或散点图；只引用字段，不生成数值。",
                input_schema=VisualizationInput, output_schema=VisualizationOutput, handler=create_visualization,
                allowed_agents=frozenset({"data-analysis-agent"}))
