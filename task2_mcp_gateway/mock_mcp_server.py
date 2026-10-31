"""A tiny downstream MCP server (JSON-RPC over HTTP POST) for testing the gateway.

It records every request it receives so tests can prove the gateway did not
forward an unauthorized call.
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

TOOLS = [
    {"name": "get_weather", "description": "Weather for a city", "inputSchema": {"type": "object"}},
    {"name": "search_docs", "description": "Search docs", "inputSchema": {"type": "object"}},
    {"name": "admin_delete_user", "description": "Delete a user", "inputSchema": {"type": "object"}},
    {"name": "admin_reset_key", "description": "Reset an API key", "inputSchema": {"type": "object"}},
]


def create_mock_app() -> FastAPI:
    app = FastAPI(title="mock-mcp-server")
    app.state.received = []  # every JSON-RPC message that reached us
    app.state.headers = []

    @app.post("/mcp")
    async def mcp(request: Request):
        msg = await request.json()
        app.state.received.append(msg)
        app.state.headers.append(dict(request.headers))
        if "id" not in msg:  # notification
            return Response(status_code=202)
        method = msg.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "mock", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = msg["params"]["name"]
            result = {"content": [{"type": "text", "text": f"ran {name}"}], "isError": False}
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": msg["id"],
                                 "error": {"code": -32601, "message": "Method not found"}})
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": result},
                            headers={"Mcp-Session-Id": "mock-session-1"})

    return app


app = create_mock_app()
