"""MCP security gateway: an authorizing reverse proxy in front of an MCP server.

Flow for every POST /mcp:
  1. Authenticate: verify the Bearer JWT and read its `role` claim.
  2. Parse the JSON-RPC body strictly (no duplicate keys, no batches).
  3. Authorize: for tools/call, a tool whose name starts with `admin_`
     requires role == "admin". Otherwise reply -32001 "Unauthorized Tool Call"
     and never contact the downstream server.
  4. Forward the *parsed* message (re-serialized), so the downstream server
     sees exactly what was authorized. tools/list and every other method pass
     through unchanged.
"""
import json
import logging
import os
import sys
import unicodedata
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .tokens import TokenError, verify

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("task2.gateway")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INTERNAL_ERROR = -32603
AUTH_REQUIRED = -32000
UNAUTHORIZED_TOOL_CALL = -32001

ADMIN_PREFIX = "admin_"
ADMIN_ROLE = "admin"

# Headers we pass to the downstream server. Authorization is deliberately
# NOT forwarded: the gateway terminates client auth.
FORWARD_REQUEST_HEADERS = {"content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id"}
FORWARD_RESPONSE_HEADERS = {"content-type", "mcp-session-id"}


def rpc_error(id_, code: int, message: str, http_status: int = 200) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}, status_code=http_status)


class DuplicateKeyError(ValueError):
    pass


def _no_duplicate_keys(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise DuplicateKeyError(k)
        obj[k] = v
    return obj


def is_admin_tool(name: str) -> bool:
    # NFKC + casefold so lookalikes such as "ADMIN_x" or full-width
    # characters cannot slip past the prefix check.
    normalized = unicodedata.normalize("NFKC", name).strip().casefold()
    return normalized.startswith(ADMIN_PREFIX)


def extract_role(request: Request, secret: str) -> str | None:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        claims = verify(token.strip(), secret)
    except TokenError as e:
        log.warning("token rejected: %s", e)
        return None
    role = claims.get("role")
    return role if isinstance(role, str) else None


def create_app(
    downstream_url: str | None = None,
    secret: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout_s: float = 30.0,
) -> FastAPI:
    downstream_url = downstream_url or os.environ.get("DOWNSTREAM_MCP_URL", "http://127.0.0.1:9001/mcp")
    secret = secret or os.environ.get("GATEWAY_JWT_SECRET", "dev-secret-change-me")
    client = httpx.AsyncClient(transport=transport, timeout=timeout_s)

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await client.aclose()

    app = FastAPI(title="mcp-security-gateway", lifespan=lifespan)

    @app.post("/mcp")
    async def proxy(request: Request):
        # 1. Authenticate before reading or forwarding anything.
        role = extract_role(request, secret)
        if role is None:
            return rpc_error(None, AUTH_REQUIRED, "Authentication required", http_status=401)

        # 2. Strict JSON-RPC parsing.
        raw = await request.body()
        try:
            msg = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
        except DuplicateKeyError:
            return rpc_error(None, INVALID_REQUEST, "Invalid Request: duplicate keys")
        except (ValueError, UnicodeDecodeError):
            return rpc_error(None, PARSE_ERROR, "Parse error")

        if isinstance(msg, list):
            return rpc_error(None, INVALID_REQUEST, "Invalid Request: batch requests are not supported")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return rpc_error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "Invalid Request")
        msg_id = msg.get("id")
        method = msg["method"]

        # 3. Method-level authorization.
        if method == "tools/call":
            params = msg.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            if not isinstance(name, str) or not name:
                return rpc_error(msg_id, INVALID_REQUEST, "Invalid Request: tools/call needs params.name")
            if is_admin_tool(name) and role != ADMIN_ROLE:
                log.warning("DENY tools/call name=%r role=%r", name, role)
                return rpc_error(msg_id, UNAUTHORIZED_TOOL_CALL, "Unauthorized Tool Call", http_status=403)
            log.info("ALLOW tools/call name=%r role=%r", name, role)
        else:
            log.info("forward method=%s role=%r", method, role)

        # 4. Forward the parsed message to the downstream server.
        headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_REQUEST_HEADERS}
        headers["content-type"] = "application/json"
        try:
            upstream_req = client.build_request("POST", downstream_url, content=json.dumps(msg), headers=headers)
            upstream = await client.send(upstream_req, stream=True)
        except httpx.HTTPError as e:
            log.error("downstream unreachable: %s", type(e).__name__)
            return rpc_error(msg_id, INTERNAL_ERROR, "Downstream MCP server unavailable", http_status=502)

        out_headers = {k: v for k, v in upstream.headers.items() if k.lower() in FORWARD_RESPONSE_HEADERS}
        if upstream.headers.get("content-type", "").startswith("text/event-stream"):
            # Streamable-HTTP servers may answer with SSE: pass bytes through as they arrive.
            async def relay():
                try:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
                finally:
                    await upstream.aclose()

            return StreamingResponse(relay(), status_code=upstream.status_code, headers=out_headers)

        body = await upstream.aread()
        await upstream.aclose()
        return Response(content=body, status_code=upstream.status_code, headers=out_headers)

    return app


app = create_app()
