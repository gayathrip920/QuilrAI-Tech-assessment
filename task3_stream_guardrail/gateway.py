"""LLM gateway that streams the model's answer back with PII redacted.

POST /v1/chat/completions (OpenAI-compatible, stream=true)
  -> forwards to UPSTREAM_LLM_URL
  -> reads the upstream SSE stream chunk by chunk
  -> runs each content delta through StreamRedactor
  -> writes redacted deltas back as SSE right away

Nothing waits for the full response. The only buffering is the SSE line
buffer (one partial line) and the redactor tail (one partial word).
"""
import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .redactor import StreamRedactor

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("task3.gateway")

MAX_SSE_LINE = 1_000_000  # protect memory from a broken upstream that never sends a newline


async def sse_events(byte_chunks: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """Turn raw bytes into SSE `data:` payloads. Handles lines split across chunks."""
    pending = b""
    async for chunk in byte_chunks:
        pending += chunk
        if len(pending) > MAX_SSE_LINE:
            raise ValueError("upstream SSE line too long")
        *lines, pending = pending.split(b"\n")
        for line in lines:
            line = line.rstrip(b"\r")
            if line.startswith(b"data:"):
                yield line[5:].strip().decode("utf-8")
    if pending.startswith(b"data:"):
        yield pending[5:].strip().decode("utf-8")


def _sse(obj) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _delta(text: str, template: dict) -> dict:
    return {**template, "choices": [{"index": 0, "delta": {"content": text}}]}


async def redact_stream(events: AsyncIterator[str]) -> AsyncIterator[str]:
    """Core pipeline: upstream SSE payloads in, redacted SSE events out."""
    redactor = StreamRedactor()
    template: dict = {}
    async for data in events:
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
        except ValueError:
            continue
        template = {k: v for k, v in event.items() if k in ("id", "object", "created", "model")}
        for choice in event.get("choices", []):
            content = (choice.get("delta") or {}).get("content")
            if content:
                safe = redactor.feed(content)
                if safe:
                    yield _sse(_delta(safe, template))
            if choice.get("finish_reason"):
                rest = redactor.flush()
                if rest:
                    yield _sse(_delta(rest, template))
                yield _sse({**template, "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}]})
    rest = redactor.flush()
    if rest:
        yield _sse(_delta(rest, template))
    yield "data: [DONE]\n\n"


def create_app(upstream_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    upstream_url = upstream_url or os.environ.get("UPSTREAM_LLM_URL", "http://127.0.0.1:9101/v1/chat/completions")
    upstream_key = os.environ.get("UPSTREAM_API_KEY", "")
    client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(10.0, read=60.0))

    @asynccontextmanager
    async def lifespan(_app):
        yield
        await client.aclose()

    app = FastAPI(title="llm-guardrail-gateway", lifespan=lifespan)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": {"type": "invalid_request", "message": "Body must be JSON"}}, status_code=400)
        body["stream"] = True
        headers = {"content-type": "application/json"}
        if upstream_key:
            headers["authorization"] = f"Bearer {upstream_key}"
        try:
            upstream = await client.send(
                client.build_request("POST", upstream_url, json=body, headers=headers), stream=True
            )
        except httpx.HTTPError as e:
            log.error("upstream unreachable: %s", type(e).__name__)
            return JSONResponse({"error": {"type": "upstream_unavailable", "message": "Model provider unavailable"}}, status_code=502)
        if upstream.status_code != 200:
            await upstream.aclose()
            return JSONResponse({"error": {"type": "upstream_error", "message": "Model provider returned an error"}}, status_code=502)

        async def body_iter():
            try:
                async for event in redact_stream(sse_events(upstream.aiter_raw())):
                    yield event
            except Exception as e:  # never leak upstream details into the stream
                log.error("stream failed: %s", type(e).__name__)
                yield _sse({"error": {"type": "stream_error", "message": "Stream interrupted"}})
            finally:
                await upstream.aclose()

        return StreamingResponse(
            body_iter(),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    return app


app = create_app()
