"""Small real MCP server used by local and automated integration tests."""
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP(
    "Data Agent MCP Test Server",
    host="127.0.0.1",
    port=18765,
    streamable_http_path="/mcp",
    json_response=True,
)


@mcp.tool(
    title="查询指标定义",
    description="按指标名称返回测试口径。",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def lookup_metric(name: str) -> dict:
    definitions = {
        "GMV": "已支付订单 amount 之和，退款订单按业务规则扣除。",
        "订单量": "支付成功且未取消的去重 order_id 数量。",
    }
    return {"name": name, "definition": definitions.get(name, "未找到该指标")}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
