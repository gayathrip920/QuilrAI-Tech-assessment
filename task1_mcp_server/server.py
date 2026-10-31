"""Customer-support MCP server over stdio.

Rules this file follows:
  * stdout carries JSON-RPC only. Every log line goes to stderr.
  * Bad input -> JSON-RPC error -32602 (Invalid params), raised as MCPError,
    so the client gets a protocol-level error, not a "successful" result.
  * Unknown tool -> -32602 as well (MCP spec convention).
  * A valid request that fails for business reasons (customer not found)
    -> a normal CallToolResult with isError=true, so the LLM can read it.
"""
import asyncio
import json
import logging
import sys
import uuid

import mcp_types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from .models import GetCustomerRecordInput, TriggerRefundInput

# Logging is configured before anything else so no handler can default to stdout.
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("task1.mcp_server")

INVALID_PARAMS = -32602

# In-memory mock data.
CUSTOMERS = {
    "CUST-00001": {"customer_id": "CUST-00001", "name": "Ada Lovelace", "tier": "gold", "balance": 120.50},
    "CUST-00002": {"customer_id": "CUST-00002", "name": "Alan Turing", "tier": "silver", "balance": 0.00},
}

TOOLS = {
    "get_customer_record": (GetCustomerRecordInput, "Get a customer record by ID (CUST-XXXXX)."),
    "trigger_refund": (TriggerRefundInput, "Trigger a refund for a customer."),
}


def _validation_error_data(err: ValidationError) -> list[dict]:
    # Only field location + message. No input echo, no internals.
    return [{"field": ".".join(str(p) for p in e["loc"]) or "(root)", "message": e["msg"]} for e in err.errors()]


def _text_result(payload: dict, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(payload))],
        is_error=is_error,
    )


def get_customer_record(args: GetCustomerRecordInput) -> types.CallToolResult:
    customer = CUSTOMERS.get(args.customer_id)
    if customer is None:
        return _text_result({"error": "customer_not_found", "customer_id": args.customer_id}, is_error=True)
    return _text_result(customer)


def trigger_refund(args: TriggerRefundInput) -> types.CallToolResult:
    if args.customer_id not in CUSTOMERS:
        return _text_result({"error": "customer_not_found", "customer_id": args.customer_id}, is_error=True)
    refund = {
        "refund_id": f"RF-{uuid.uuid4().hex[:12]}",
        "customer_id": args.customer_id,
        "amount": args.amount,
        "reason": args.reason,
        "status": "processed",
    }
    log.info("refund issued customer=%s amount=%s", args.customer_id, args.amount)
    return _text_result(refund)


HANDLERS = {"get_customer_record": get_customer_record, "trigger_refund": trigger_refund}


async def handle_list_tools(ctx, params) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(name=name, description=desc, input_schema=model.model_json_schema())
            for name, (model, desc) in TOOLS.items()
        ]
    )


def dispatch(name: str, arguments: dict | None) -> types.CallToolResult:
    """Validate and run one tool call. Raises MCPError(-32602) on bad input."""
    if name not in TOOLS:
        raise MCPError(code=INVALID_PARAMS, message=f"Unknown tool: {name}")
    model, _ = TOOLS[name]
    try:
        args = model.model_validate(arguments or {})
    except ValidationError as e:
        log.warning("invalid params for %s: %d error(s)", name, e.error_count())
        raise MCPError(code=INVALID_PARAMS, message="Invalid params", data=_validation_error_data(e)) from None
    return HANDLERS[name](args)


async def handle_call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    log.info("tools/call name=%s", params.name)
    return dispatch(params.name, params.arguments)


server = Server(
    "customer-support",
    version="1.0.0",
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


async def main() -> None:
    log.info("starting MCP server on stdio")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
