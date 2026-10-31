"""LLM gateway: per-tenant token rate limiting + primary/backup failover.

POST /v1/chat/completions with header "Authorization: Bearer <tenant api key>".

Every error the client sees has the same shape:
    {"error": {"type": "...", "message": "...", "request_id": "..."}}
No upstream error text, hostnames or stack traces are ever returned.
"""
import json
import logging
import math
import os
import sys
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .limiter import SlidingWindowLimiter
from .router import AllProvidersFailed, ClientRequestRejected, FallbackRouter, Provider

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("task4.app")

DEFAULT_LIMIT = 50_000
DEFAULT_MAX_TOKENS = 256

# Demo tenants. In production these would live in a database, with the keys hashed.
DEFAULT_TENANTS = {
    "key-tenant-a": {"tenant": "tenant-a", "tokens_per_minute": DEFAULT_LIMIT},
    "key-tenant-b": {"tenant": "tenant-b", "tokens_per_minute": 5_000},
}


def gateway_error(status: int, type_: str, message: str, request_id: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"type": type_, "message": message, "request_id": request_id}},
        status_code=status,
        headers={"x-request-id": request_id, **(headers or {})},
    )


def estimate_tokens(payload: dict) -> int:
    """Rough upper bound before the call: ~4 chars per token for the prompt,
    plus the most the model may generate. Corrected after the call with the
    provider's real usage numbers."""
    text = json.dumps(payload.get("messages", []))
    max_tokens = payload.get("max_tokens", DEFAULT_MAX_TOKENS)
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        max_tokens = DEFAULT_MAX_TOKENS
    return math.ceil(len(text) / 4) + max_tokens


def create_app(
    db_path: str | None = None,
    primary: Provider | None = None,
    backup: Provider | None = None,
    tenants: dict | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    primary_timeout_s: float = 3.0,
    clock=None,
) -> FastAPI:
    db_path = db_path or os.environ.get("RATE_LIMIT_DB", "rate_limits.db")
    primary = primary or Provider("primary", os.environ.get("PRIMARY_URL", "http://127.0.0.1:9201/v1/chat/completions"),
                                  os.environ.get("PRIMARY_API_KEY", ""))
    backup = backup or Provider("backup", os.environ.get("BACKUP_URL", "http://127.0.0.1:9202/v1/chat/completions"),
                                os.environ.get("BACKUP_API_KEY", ""))
    tenants = tenants or DEFAULT_TENANTS
    limiter = SlidingWindowLimiter(db_path, **({"clock": clock} if clock else {}))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await limiter.open()
        app.state.client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30.0, connect=2.0))
        app.state.router = FallbackRouter(primary, backup, app.state.client, primary_timeout_s=primary_timeout_s)
        yield
        await app.state.client.aclose()
        await limiter.close()

    app = FastAPI(title="llm-router", lifespan=lifespan)
    app.state.limiter = limiter

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", uuid.uuid4().hex)
        log.exception("unhandled error request_id=%s", rid)  # full detail stays in our logs
        return gateway_error(500, "internal_error", "Internal gateway error", rid)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        rid = uuid.uuid4().hex
        request.state.request_id = rid

        auth = request.headers.get("authorization", "")
        scheme, _, key = auth.partition(" ")
        tenant_cfg = tenants.get(key.strip()) if scheme.lower() == "bearer" else None
        if tenant_cfg is None:
            return gateway_error(401, "authentication_error", "Invalid or missing API key", rid)

        try:
            payload = await request.json()
        except ValueError:
            return gateway_error(400, "invalid_request", "Body must be valid JSON", rid)
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            return gateway_error(400, "invalid_request", "Body must include a 'messages' list", rid)
        payload["stream"] = False

        tenant, limit = tenant_cfg["tenant"], tenant_cfg["tokens_per_minute"]
        estimate = estimate_tokens(payload)
        decision = await limiter.reserve(tenant, estimate, limit)
        rate_headers = {"x-ratelimit-limit-tokens": str(limit)}
        if not decision.allowed:
            retry = max(1, math.ceil(decision.retry_after_s))
            log.info("rate limited tenant=%s used=%d need=%d limit=%d", tenant, decision.used, estimate, limit)
            return gateway_error(429, "rate_limit_exceeded", "Token rate limit exceeded for this API key", rid,
                                 {**rate_headers, "retry-after": str(retry)})

        try:
            result = await app.state.router.route(payload)
        except AllProvidersFailed:
            await limiter.release(decision.reservation_id)
            return gateway_error(503, "upstream_unavailable", "All model providers are unavailable. Try again later.", rid)
        except ClientRequestRejected:
            await limiter.release(decision.reservation_id)
            return gateway_error(400, "invalid_request", "The model provider rejected the request", rid)
        except BaseException:
            await limiter.release(decision.reservation_id)
            raise

        usage = result.body.get("usage") if isinstance(result.body, dict) else None
        actual = usage.get("total_tokens") if isinstance(usage, dict) else None
        if isinstance(actual, int) and not isinstance(actual, bool):
            await limiter.reconcile(decision.reservation_id, actual)

        return JSONResponse(
            result.body,
            headers={**rate_headers, "x-request-id": rid, "x-gateway-provider": result.provider},
        )

    return app
